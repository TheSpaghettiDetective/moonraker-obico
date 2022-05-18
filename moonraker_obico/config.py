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
    host: str = '127.0.0.1'
    port: int = 7125
    api_key: Optional[str] = None

    def http_address(self):
        if not self.host or not self.port:
            return None
        return f'http://{self.host}:{self.port}'

    def ws_url(self):
        return f'ws://{self.host}:{self.port}/websocket'


@dataclasses.dataclass
class ServerConfig:
    url: str = 'https://app.obico.io'
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
    disable_video_streaming: bool = False
    snapshot_url: str = ''
    snapshot_ssl_validation: bool = False
    stream_url: str = ''
    flip_h: bool = False
    flip_v: bool = False
    rotate_90: bool = False
    aspect_ratio_169: bool = False

    def update_from_moonraker(self, mr_conn):

        # Check for the standard namespace for webcams
        result = mr_conn.api_get('server.database.item', raise_for_status=False, namespace='webcams')

        if result:
            # TODO: Just pick the last webcam before we have a way to support multiple cameras
            for cfg in result.get('value', {}).values():
                self.snapshot_url = self.webcam_full_url(cfg.get('urlSnapshot', None))
                self.stream_url = self.webcam_full_url(cfg.get('urlStream', None))
                self.flip_h = cfg.get('flipX', False)
                self.flip_v = cfg.get('flipY', False)

            return

        # webcam configs not found in the standard location. Try fluidd's flavor
        result = mr_conn.api_get('server.database.item', raise_for_status=False, namespace='fluidd', key='cameras')
        if result:
            # TODO: Just pick the last webcam before we have a way to support multiple cameras
            for cfg in result.get('value', {}).get('cameras', []):
                if not cfg.get('enabled', False):
                    continue

                self.stream_url = self.webcam_full_url(cfg.get('url', None))
                self.flip_h = cfg.get('flipX', False)
                self.flip_v = cfg.get('flipY', False)

            return

        #TODO: Send notification to user that webcam configs not found when moonraker's announcement api makes to stable


    @classmethod
    def webcam_full_url(cls, url):
        if not url or not url.strip():
            return ''

        full_url = url.strip()
        if not urlparse(full_url).scheme:
            full_url = "http://localhost/" + re.sub(r"^\/", "", full_url)

        return full_url


@dataclasses.dataclass
class LoggingConfig:
    path: str
    level: str = 'DEBUG'


@dataclasses.dataclass
class Config:
    moonraker: MoonrakerConfig
    server: ServerConfig
    webcam: WebcamConfig
    logging: LoggingConfig

    _config_path: str
    _config: ConfigParser

    sentry_opt: str = 'out'

    def write(self) -> None:
        with open(self._config_path, 'w') as f:
            self._config.write(f)

    def update_tsd_auth_token(self, auth_token: str):
        self.server.auth_token = auth_token
        self._config.set('server', 'auth_token', auth_token)
        self.write()

    @classmethod
    def load_from(cls, config_path: str) -> 'Config':
        config = ConfigParser()
        config.read([config_path, ])

        moonraker_config = MoonrakerConfig(
            host=config.get(
                'moonraker', 'host',
                fallback='127.0.0.1'
            ),
            port=config.get(
                'moonraker', 'port',
                fallback=7125
            ),
            api_key=config.get(
                'moonraker', 'api_key',
                fallback=None
            ),
        )

        tsd_config = ServerConfig(
            url=config.get(
                'server', 'url',
                fallback='https://app.obico.io'),
            auth_token=config.get(
                'server', 'auth_token',
                fallback=None),
            upload_dir=config.get(
                'server', 'upload_dir',
                fallback='Obico_Upload').strip().lstrip('/').rstrip('/'),
            feedrate_xy=config.getint(
                'server', 'feedrate_xy',
                fallback=ServerConfig.DEFAULT_FEEDRATE_XY,
            ),
            feedrate_z=config.getint(
                'server', 'feedrate_z',
                fallback=ServerConfig.DEFAULT_FEEDRATE_Z,
            )
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

        logging_config = LoggingConfig(
            path=config.get(
                'logging', 'path',
                fallback=''
            ),
            level=config.get(
                'logging', 'level',
                fallback=''
            ),
		)

        sentry_opt = config.get(
            'misc', 'sentry_opt',
            fallback='out'
        )

        return Config(
            moonraker=moonraker_config,
            server=tsd_config,
            webcam=webcam_config,
            logging=logging_config,
            _config=config,
            _config_path=config_path,
            sentry_opt=sentry_opt,
        )

    def get_sentry(self):
        sentryClient = raven.Client(
            'https://89fc4cf9318d46b1bfadc03c9d34577c@sentry.obico.io/8',  # noqa
            release=VERSION,
            ignore_exceptions=[]
        ) if self.sentry_opt == 'in' else None
        sentry = SentryWrapper(sentryClient)
        sentry.tags_context(get_tags())
        if self.server.auth_token:
            sentry.user_context(
                {'id': self.server.auth_token}
            )
        return sentry
