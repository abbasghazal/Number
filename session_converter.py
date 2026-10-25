import base64
import binascii
import ipaddress
import struct

class ValidationError(Exception):
    pass

class TeleSession:
    _STRUCT_PREFORMAT = '>B{}sH256s'
    CURRENT_VERSION = '1'

    def __init__(self, *, dc_id: int, auth_key: bytes, **kwargs):
        self.dc_id = dc_id
        self.auth_key = auth_key
        self.server_address = kwargs.get('server_address')
        self.port = kwargs.get('port')
        self.takeout_id = kwargs.get('takeout_id')

    @classmethod
    def from_string(cls, string: str):
        string = cls.normalize_string(string)
        if not string or not string.startswith(cls.CURRENT_VERSION):
            raise ValidationError("Unsupported Telethon session format")
        encoded = string[1:]
        payload = base64.urlsafe_b64decode(encoded + '=' * (-len(encoded) % 4))
        ipv4_size = struct.calcsize(cls._STRUCT_PREFORMAT.format(4))
        ipv6_size = struct.calcsize(cls._STRUCT_PREFORMAT.format(16))
        if len(payload) == ipv4_size:
            ip_len = 4
        elif len(payload) == ipv6_size:
            ip_len = 16
        else:
            raise ValidationError("Invalid Telethon session payload length")
        dc_id, ip, port, auth_key = struct.unpack(
            cls._STRUCT_PREFORMAT.format(ip_len), payload)
        server_address = ipaddress.ip_address(ip).compressed
        return cls(
            dc_id=dc_id,
            auth_key=auth_key,
            port=port,
            server_address=server_address
        )

    @classmethod
    def normalize_string(cls, string: str) -> str:
        """Normalize copied Telethon strings for Telethon versions requiring padding."""
        string = "".join(str(string or "").replace("`", "").split())
        if not string or not string.startswith(cls.CURRENT_VERSION):
            return string
        payload = string[1:]
        return cls.CURRENT_VERSION + payload + "=" * (-len(payload) % 4)

    def to_string(self) -> str:
        ip = ipaddress.ip_address(self.server_address).packed
        packed = struct.pack(
            self._STRUCT_PREFORMAT.format(len(ip)),
            self.dc_id,
            ip,
            self.port,
            self.auth_key
        )
        # Telethon's StringSession.decode passes this directly to base64, so
        # retain the standard Base64 padding rather than stripping it.
        return self.CURRENT_VERSION + base64.urlsafe_b64encode(packed).decode()

class PyroSession:
    STRING_FORMAT = ">BI?256sQ?"
    LEGACY_STRING_FORMAT = ">B?256sI?"

    def __init__(self, *, dc_id: int, auth_key: bytes, **kwargs):
        self.dc_id = dc_id
        self.auth_key = auth_key
        self.user_id = kwargs.get('user_id')
        self.is_bot = kwargs.get('is_bot', False)
        self.test_mode = kwargs.get('test_mode', False)
        self.api_id = kwargs.get('api_id')

    @classmethod
    def from_string(cls, session_string: str):
        # Sessions are often copied from a Markdown code block or with spaces.
        # Base64 decoding must receive the raw compact session string only.
        session_string = "".join(str(session_string or "").replace("`", "").split())
        if not session_string:
            raise ValidationError("Empty Pyrogram session")
        try:
            data = base64.urlsafe_b64decode(session_string + '=' * (-len(session_string) % 4))
        except (binascii.Error, ValueError) as exc:
            raise ValidationError("Invalid or incomplete Pyrogram session") from exc

        if len(data) == struct.calcsize(cls.STRING_FORMAT):
            dc_id, api_id, test_mode, auth_key, user_id, is_bot = struct.unpack(cls.STRING_FORMAT, data)
        elif len(data) == struct.calcsize(cls.LEGACY_STRING_FORMAT):
            dc_id, test_mode, auth_key, user_id, is_bot = struct.unpack(cls.LEGACY_STRING_FORMAT, data)
            api_id = None
        else:
            raise ValidationError("Unsupported Pyrogram session format")
        return cls(
            dc_id=dc_id,
            api_id=api_id,
            auth_key=auth_key,
            user_id=user_id,
            is_bot=is_bot,
            test_mode=test_mode
        )

    def to_string(self) -> str:
        packed = struct.pack(
            self.STRING_FORMAT,
            self.dc_id,
            self.api_id or 0,
            self.test_mode,
            self.auth_key,
            self.user_id or 0,
            self.is_bot
        )
        return base64.urlsafe_b64encode(packed).decode().rstrip('=')

class MangSession:
    # Pyrogram string sessions store the DC id but not its network endpoint.
    # Telethon string sessions require both, so use Telegram's public IPv4 DCs.
    _DC_ENDPOINTS = {
        1: ("149.154.175.50", 443),
        2: ("149.154.167.51", 443),
        3: ("149.154.175.100", 443),
        4: ("149.154.167.91", 443),
        5: ("91.108.56.130", 443),
    }

    @staticmethod
    def PYROGRAM_TO_TELETHON(session_string: str):
        pyro_session = PyroSession.from_string(session_string)
        endpoint = MangSession._DC_ENDPOINTS.get(pyro_session.dc_id)
        if not endpoint:
            raise ValidationError(f"Unsupported Telegram data center: {pyro_session.dc_id}")
        tele_session = TeleSession(
            dc_id=pyro_session.dc_id,
            auth_key=pyro_session.auth_key,
            server_address=endpoint[0],
            port=endpoint[1],
        )
        return tele_session.to_string()

    @staticmethod
    def TELETHON_TO_PYROGRAM(session_string: str):
        tele_session = TeleSession.from_string(session_string)
        pyro_session = PyroSession(
            dc_id=tele_session.dc_id,
            auth_key=tele_session.auth_key
        )
        return pyro_session.to_string()
