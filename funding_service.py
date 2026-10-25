"""Immediate rewards and continuing membership monitoring for bot and web."""

import asyncio
import datetime as dt
import logging
from telethon import types
import promotion_store as store
from promotion_service import is_member
from utils.asyncio_compat import to_thread
from workflow import query

LOG = logging.getLogger(__name__)


async def verify(bot, user, task):
    row = await query(
        "SELECT c.target_id,t.status,t.check_due,c.guarantee_seconds FROM funding_tasks t JOIN funding_campaigns c ON c.campaign_id=t.campaign_id WHERE t.task_id=? AND t.user_id=?",
        (task, user),
        fetchone=True,
    )
    if not row:
        raise ValueError("المهمة ليست ضمن حسابك.")
    if row[1] == "completed":
        return dict(reward=0, status="completed", check_due=row[2])
    if not await is_member(bot, row[0], user):
        await query(
            "INSERT INTO funding_verifications(task_id,user_id,phase,result) VALUES(?,?,'initial','not_member')",
            (task, user),
            commit=True,
        )
        raise ValueError("لم يتم تأكيد الاشتراك. انضم ثم أعد التحقق.")
    reward = await to_thread(store.reward_task, user, task)
    status = await query(
        "SELECT status,check_due FROM funding_tasks WHERE task_id=? AND user_id=?", (task, user), fetchone=True
    )
    return dict(reward=reward, status=status[0], check_due=status[1])


async def dispatch(bot):
    rows = (
        await query(
            """UPDATE funding_tasks SET lease_until=CURRENT_TIMESTAMP+INTERVAL '2 minutes'
        WHERE task_id IN (SELECT task_id FROM funding_tasks WHERE status='pending'
        AND (lease_until IS NULL OR lease_until<CURRENT_TIMESTAMP) ORDER BY check_due LIMIT 25 FOR UPDATE SKIP LOCKED)
        RETURNING task_id,user_id,campaign_id""",
            commit=True,
            fetchall=True,
        )
        or []
    )
    for task, user, campaign in rows:
        try:
            target = await query(
                "SELECT target_id FROM funding_campaigns WHERE campaign_id=?", (campaign,), fetchone=True
            )
            member = await asyncio.wait_for(is_member(bot, target[0], user), 15)
            await to_thread(store.final_reward, user, task, member)
            await to_thread(store.settle_campaign, campaign)
        except asyncio.CancelledError:
            raise
        except Exception:
            await query(
                "UPDATE funding_tasks SET lease_until=CURRENT_TIMESTAMP+INTERVAL '5 minutes',last_result='verification_unavailable' WHERE task_id=? AND status='pending'",
                (task,),
                commit=True,
            )
    for (key,) in (
        await query(
            "SELECT campaign_id FROM funding_campaigns WHERE status='pausing' ORDER BY updated_at LIMIT 50",
            fetchall=True,
        )
        or []
    ):
        await to_thread(store.settle_campaign, key)
    await monitor_memberships(bot)


async def monitor_memberships(bot):
    rows = (
        await query(
            """UPDATE funding_membership_watch SET lease_until=CURRENT_TIMESTAMP+INTERVAL '2 minutes'
        WHERE watch_id IN (SELECT watch_id FROM funding_membership_watch WHERE status='active' AND next_check<=CURRENT_TIMESTAMP
        AND (lease_until IS NULL OR lease_until<CURRENT_TIMESTAMP) ORDER BY next_check,watch_id LIMIT 25 FOR UPDATE SKIP LOCKED)
        RETURNING watch_id,user_id,target_id""",
            commit=True,
            fetchall=True,
        )
        or []
    )
    for key, user, target in rows:
        try:
            member = await asyncio.wait_for(is_member(bot, target, user), 15)
            if not member:
                await to_thread(store.penalize_departure, user, key)
            else:
                await query(
                    "UPDATE funding_membership_watch SET next_check=CURRENT_TIMESTAMP+INTERVAL '5 minutes',lease_until=NULL,last_checked=CURRENT_TIMESTAMP,last_result='member' WHERE watch_id=? AND status='active'",
                    (key,),
                    commit=True,
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            # An unavailable membership check is not proof of departure.
            await query(
                "UPDATE funding_membership_watch SET next_check=CURRENT_TIMESTAMP+INTERVAL '5 minutes',lease_until=NULL,last_result='verification_unavailable' WHERE watch_id=? AND status='active'",
                (key,),
                commit=True,
            )


def absent(participant):
    return (
        participant is None
        or isinstance(participant, types.ChannelParticipantLeft)
        or isinstance(participant, types.ChannelParticipantBanned)
        and participant.left
    )


async def membership_update(update):
    if (
        not isinstance(update, types.UpdateChannelParticipant)
        or not absent(update.new_participant)
        or absent(update.prev_participant)
    ):
        return
    if not isinstance(update.date, dt.datetime):
        return
    observed = update.date if update.date.tzinfo else update.date.replace(tzinfo=dt.timezone.utc)
    try:
        row = await query(
            "SELECT watch_id FROM funding_membership_watch WHERE target_id=? AND user_id=? AND status='active'",
            (-1000000000000 - update.channel_id, update.user_id),
            fetchone=True,
        )
        if row:
            await to_thread(store.penalize_departure, update.user_id, row[0], observed_at=observed)
    except asyncio.CancelledError:
        raise
    except Exception:
        LOG.warning("Membership update deferred to the persisted monitor")
