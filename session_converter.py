import base64
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
        string = string[1:]
        ip_len = 4 if len(string) == 352 else 16
        dc_id, ip, port, auth_key = struct.unpack(
            cls._STRUCT_PREFORMAT.format(ip_len), 
            base64.urlsafe_b64decode(string + '=' * (-len(string) % 4)))
        server_address = ipaddress.ip_address(ip).compressed
        return cls(
            dc_id=dc_id,
            auth_key=auth_key,
            port=port,
            server_address=server_address
        )

    def to_string(self) -> str:
        ip = ipaddress.ip_address(self.server_address).packed
        packed = struct.pack(
            self._STRUCT_PREFORMAT.format(len(ip)),
            self.dc_id,
            ip,
            self.port,
            self.auth_key
        )
        return self.CURRENT_VERSION + base64.urlsafe_b64encode(packed).decode().rstrip('=')

class PyroSession:
    STRING_FORMAT = ">BI?256sQ?"

    def __init__(self, *, dc_id: int, auth_key: bytes, **kwargs):
        self.dc_id = dc_id
        self.auth_key = auth_key
        self.user_id = kwargs.get('user_id')
        self.is_bot = kwargs.get('is_bot', False)
        self.test_mode = kwargs.get('test_mode', False)
        self.api_id = kwargs.get('api_id')

    @classmethod
    def from_string(cls, session_string: str):
        data = base64.urlsafe_b64decode(session_string + '=' * (-len(session_string) % 4))
        dc_id, api_id, test_mode, auth_key, user_id, is_bot = struct.unpack(cls.STRING_FORMAT, data)
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
    @staticmethod
    def PYROGRAM_TO_TELETHON(session_string: str):
        pyro_session = PyroSession.from_string(session_string)
        tele_session = TeleSession(
            dc_id=pyro_session.dc_id,
            auth_key=pyro_session.auth_key
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