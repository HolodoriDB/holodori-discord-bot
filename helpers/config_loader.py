from typing import NotRequired, TypedDict

import yaml


class ConfigDiscord(TypedDict):
    name: str
    token: str
    owner_ids: list[int]
    support_invite: str
    support_id: int
    tos_url: NotRequired[str]
    privacy_url: NotRequired[str]
    donate_url: NotRequired[str]


class ConfigHolodori(TypedDict):
    api_url: str
    bypass_header: str
    bypass_value: str
    lang: str  # default display language (eng/jpn/kor/cht/chs/ind)
    regions: list[str]  # event regions (us/as/jp)
    refresh_interval: int


class ConfigGuessing(TypedDict):
    enabled: bool


class ConfigPSQL(TypedDict):
    host: str
    user: str
    database: str
    port: int
    password: str
    pool_min_size: int
    pool_max_size: int


class Config(TypedDict):
    discord: ConfigDiscord
    holodori: ConfigHolodori
    guessing: NotRequired[ConfigGuessing]
    psql: ConfigPSQL


_config: Config | None = None
_config_path: str = "config.yml"


def set_config_path(path: str) -> None:
    global _config_path, _config
    _config_path = path
    _config = None


def get_config() -> Config:
    global _config
    if _config is None:
        with open(_config_path, "r", encoding="utf-8") as f:
            _config = yaml.load(f, yaml.Loader)
    assert _config is not None
    return _config
