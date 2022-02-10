import dataclasses
from typing import Optional
import re
from configparser import ConfigParser

import raven  # type: ignore
from .version import VERSION
from .utils import SentryWrapper, get_tags


@dataclasses.dataclass
class MoonrakerConfig:
    url: str = 'http://127.0.0.1:7125'
    api_key: Optional[str] = None

    def canonical_endpoint_prefix(self):
        if not self.url:
            return None

        endpoint_prefix = self.url.strip()
        if endpoint_prefix.endswith('/'):
            endpoint_prefix = endpoint_prefix[:-1]

        return endpoint_prefix

    def canonical_ws_prefix(self):
        return re.sub(r'^http', 'ws', self.canonical_endpoint_prefix())

    def ws_url(self):
        return f'{self.canonical_ws_prefix()}/websocket'


@dataclasses.dataclass
class TSDConfig:
    url: str = 'https://app.thespaghettidetective.com'
    auth_token: Optional[str] = None

    # disable_video_streaming: bool = False
    # pi_cam_resolution: str = 'medium'
    # video_streaming_compatible_mode: str = 'auto'

    def canonical_endpoint_prefix(self):
        if not self.url:
            return None

        endpoint_prefix = self.url.strip()
        if endpoint_prefix.endswith('/'):
            endpoint_prefix = endpoint_prefix[:-1]

        return endpoint_prefix

    def canonical_ws_prefix(self):
        return re.sub(r'^http', 'ws', self.canonical_endpoint_prefix())

    def ws_url(self):
        return f'{self.canonical_ws_prefix()}/ws/dev/'


@dataclasses.dataclass
class WebcamConfig:
    snapshot_url: str = ''
    snapshot_ssl_validation: bool = False
    stream_url: str = ''
    flip_h: bool = False
    flip_v: bool = False
    rotate_90: bool = False
    aspect_ratio_169: bool = False


@dataclasses.dataclass
class Config:
    moonraker: MoonrakerConfig
    thespaghettidetective: TSDConfig
    webcam: WebcamConfig

    _config_path: str
    _config: ConfigParser

    sentry_opt: str = 'out'

    def write(self) -> None:
        with open(self._config_path, 'w') as f:
            self._config.write(f)

    def update_tsd_auth_token(self, auth_token: str):
        self.thespaghettidetective.auth_token = auth_token
        self._config.set('thespaghettidetective', 'auth_token', auth_token)
        self.write()

    @classmethod
    def load_from(cls, config_path: str) -> 'Config':
        config = ConfigParser()
        config.read([config_path, ])
        return cls.from_config(config, config_path)

    @classmethod
    def from_config(cls, config: ConfigParser, config_path: str) -> 'Config':
        moonraker_config = MoonrakerConfig(
            url=config.get(
                'moonraker', 'url',
                fallback='http://127.0.0.1:7125'
            ),
            api_key=config.get(
                'moonraker', 'api_key',
                fallback=None
            ),
        )

        tsd_config = TSDConfig(
            url=config.get(
                'thespaghettidetective', 'url',
                fallback='https://app.thespaghettidetective.com'),
            auth_token=config.get(
                'thespaghettidetective', 'auth_token',
                fallback=None)
        )

        webcam_config = WebcamConfig(
            snapshot_url=config.get(
                'webcam', 'snapshot_url',
                fallback=''),
            snapshot_ssl_validation=config.getboolean(
                'webcam', 'snapshot_ssl_validation',
                fallback=False
            ),
            stream_url=config.get(
                'webcam', 'stream_url',
                fallback='http://127.0.0.1:8080/?action=stream'
            ),
            flip_h=config.getboolean(
                'webcam', 'flip_h',
                fallback=False
            ),
            flip_v=config.getboolean(
                'webcam', 'flip_v',
                fallback=False
            ),
            rotate_90=config.getboolean(
                'webcam', 'rotate_90',
                fallback=False
            ),
            aspect_ratio_169=config.getboolean(
                'webcam', 'aspect_ratio_169',
                fallback=False
            )
        )

        sentry_opt = config.get(
            'thespaghettidetective', 'sentry_opt',
            fallback='out'
        )

        return Config(
            moonraker=moonraker_config,
            thespaghettidetective=tsd_config,
            webcam=webcam_config,
            _config=config,
            _config_path=config_path,
            sentry_opt=sentry_opt,
        )

    def get_sentry(self):
        sentryClient = raven.Client(
            'https://89fc4cf9318d46b1bfadc03c9d34577c@sentry.thespaghettidetective.com/8',  # noqa
            release=VERSION,
            ignore_exceptions=[]
        ) if self.sentry_opt == 'in' else None
        sentry = SentryWrapper(sentryClient)
        sentry.tags_context(get_tags())
        if self.thespaghettidetective.auth_token:
            sentry.user_context(
                {'id': self.thespaghettidetective.auth_token}
            )
        return sentry
