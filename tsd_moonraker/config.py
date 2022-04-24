import dataclasses
from typing import Optional
import re
from configparser import ConfigParser
from urllib.parse import urlparse

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
    upload_dir: str = ''  # relative to virtual sdcard

    # feedrates for printer control, mm/s
    DEFAULT_FEEDRATE_XY = 100
    DEFAULT_FEEDRATE_Z = 10
    feedrate_xy : int = DEFAULT_FEEDRATE_XY
    feedrate_z : int = DEFAULT_FEEDRATE_Z

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

    def update_from_moonraker(self, mr_conn):
        result = mr_conn.api_get('server.database.item', namespace='webcams')

        # TODO: Just pick the last webcam before we have a way to support multiple cameras
        for cfg in result.get('value', {}).values():
            self.snapshot_url = self.webcam_full_url(cfg.get('urlSnapshot', None))
            self.stream_url = self.webcam_full_url(cfg.get('urlStream', None))
            self.flip_h = cfg.get('flipX', False)
            self.flip_v = cfg.get('flipY', False)

    @classmethod
    def webcam_full_url(cls, url):
        if not url or not url.strip():
            return ''

        full_url = url.strip()
        if not urlparse(full_url).scheme:
            full_url = "http://localhost/" + re.sub(r"^\/", "", full_url)

        return full_url


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
                fallback=None),
            upload_dir=config.get(
                'thespaghettidetective', 'upload_dir',
                fallback='thespaghettidetective').strip().lstrip('/').rstrip('/'),
            feedrate_xy=config.getint(
                'thespaghettidetective', 'feedrate_xy',
                fallback=TSDConfig.DEFAULT_FEEDRATE_XY,
            ),
            feedrate_z=config.getint(
                'thespaghettidetective', 'feedrate_z',
                fallback=TSDConfig.DEFAULT_FEEDRATE_Z,
            )
        )

        webcam_config = WebcamConfig()

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
