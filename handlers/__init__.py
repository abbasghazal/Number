# Auto-split handlers package. Public imports are kept compatible with the old handlers.py module.
import sys
import types as _module_types

from . import base as _base
from . import common as _common
from . import auth_license as _auth_license
from . import user_handlers as _user_handlers
from . import account_handlers as _account_handlers
from . import account_services as _account_services
from . import storage_handlers as _storage_handlers
from . import settings_handlers as _settings_handlers
from . import admin_handlers as _admin_handlers
from . import balance_handlers as _balance_handlers
from . import payment_handlers as _payment_handlers
from . import buy_sell_handlers as _buy_sell_handlers
from . import country_handlers as _country_handlers
from . import posting_handlers as _posting_handlers
from . import creation_handlers as _creation_handlers
from . import leave_handlers as _leave_handlers
from . import profile_handlers as _profile_handlers
from . import support_handlers as _support_handlers
from . import update_handlers as _update_handlers
from . import auction_handlers as _auction_handlers
from . import callback_router as _callback_router
from . import registry as _registry

_MODULES = [
    _base,
    _common,
    _auth_license,
    _user_handlers,
    _account_handlers,
    _account_services,
    _storage_handlers,
    _settings_handlers,
    _admin_handlers,
    _balance_handlers,
    _payment_handlers,
    _buy_sell_handlers,
    _country_handlers,
    _posting_handlers,
    _creation_handlers,
    _leave_handlers,
    _profile_handlers,
    _support_handlers,
    _update_handlers,
    _auction_handlers,
    _callback_router,
    _registry,
]

_PUBLIC = {}
for _module in _MODULES:
    _PUBLIC.update({
        _name: _value
        for _name, _value in vars(_module).items()
        if not _name.startswith("_") and (
            _module is _base or getattr(_value, '__module__', None) == _module.__name__
        )
    })

# The original file used one module-level namespace. After splitting, this keeps
# cross-handler calls and monkey-patched globals such as handlers.bot compatible.
for _module in _MODULES:
    for _name, _value in _PUBLIC.items():
        _module.__dict__.setdefault(_name, _value)

globals().update(_PUBLIC)


class _HandlersModule(_module_types.ModuleType):
    def __setattr__(self, name, value):
        super().__setattr__(name, value)
        for module in _MODULES:
            if name in module.__dict__:
                module.__dict__[name] = value


sys.modules[__name__].__class__ = _HandlersModule

__all__ = sorted(_PUBLIC)
