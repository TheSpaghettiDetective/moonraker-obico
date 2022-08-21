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

    def __init__(self, webcam_config_section):
        self.webcam_config_section = webcam_config_section
        self.moonraker_webcam_config = {}

    def update_from_moonraker(self, mr_conn):

        # Check for the standard namespace for webcams
        result = mr_conn.api_get('server.database.item', raise_for_status=False, namespace='webcams')

        if result:
            # TODO: Just pick the last webcam before we have a way to support multiple cameras
            for cfg in result.get('value', {}).values():
                self.moonraker_webcam_config = dict(
                    snapshot_url = cfg.get('urlSnapshot', None),
                    stream_url = cfg.get('urlStream', None),
                    flip_h = cfg.get('flipX', False),
                    flip_v = cfg.get('flipY', False),
                )
            return

        # webcam configs not found in the standard location. Try fluidd's flavor
        result = mr_conn.api_get('server.database.item', raise_for_status=False, namespace='fluidd', key='cameras')
        if result:
            # TODO: Just pick the last webcam before we have a way to support multiple cameras
            for cfg in result.get('value', {}).get('cameras', []):
                if not cfg.get('enabled', False):
                    continue

                self.moonraker_webcam_config = dict(
                    stream_url = self.webcam_full_url(cfg.get('url', None)),
                    flip_h = cfg.get('flipX', False),
                    flip_v = cfg.get('flipY', False),
                )
            return

        #TODO: Send notification to user that webcam configs not found when moonraker's announcement api makes to stable


    @property
    def snapshot_url(self):
        return self.webcam_config_section.get('snapshot_url') or self.moonraker_webcam_config.get('snapshot_url')

    @property
    def disable_video_streaming(self):
        return self.webcam_config_section.getboolean('disable_video_streaming', False)

    @property
    def snapshot_ssl_validation(self):
        return False

    @property
    def stream_url(self):
        return self.webcam_config_section.get('stream_url') or self.moonraker_webcam_config.get('stream_url')

    @property
    def flip_h(self):
        return self.webcam_config_section.getboolean('flip_h') or self.moonraker_webcam_config.get('flip_h')

    @property
    def flip_v(self):
        return self.webcam_config_section.getboolean('flip_v') or self.moonraker_webcam_config.get('flip_v')

    @property
    def rotate_90(self):
        return self.webcam_config_section.getboolean('rotate_90', False)

    @property
    def aspect_ratio_169(self):
        return self.webcam_config_section.getboolean('aspect_ratio_169', False)

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


class Config:

    def __init__(self, config_path: str):
        self._heater_mapping = {}

        self._config_path = config_path
        config = ConfigParser()
        config.read([config_path, ])

        self.moonraker = MoonrakerConfig(
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

        self.server = ServerConfig(
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

        self.webcam = WebcamConfig(webcam_config_section=config['webcam'])

        self.logging = LoggingConfig(
            path=config.get(
                'logging', 'path',
                fallback=''
            ),
            level=config.get(
                'logging', 'level',
                fallback=''
            ),
		)

        self.sentry_opt = config.get(
            'misc', 'sentry_opt',
            fallback='out'
        )


    def write(self) -> None:
        with open(self._config_path, 'w') as f:
            self._config.write(f)

    def update_tsd_auth_token(self, auth_token: str):
        self.server.auth_token = auth_token
        self._config.set('server', 'auth_token', auth_token)
        self.write()

    def update_heater_mapping(self, available_heaters):
        tool_no = 0
        for heater in sorted(available_heaters):
            if heater == "heater_bed":
                self._heater_mapping['heater_bed'] = 'bed'
            else:
                self._heater_mapping[heater] = f'tool{tool_no}'
                tool_no += 1

    def get_mapped_server_heater_name(self, mr_heater_name):
        return self._heater_mapping.get(mr_heater_name)

    def get_mapped_mr_heater_name(self, server_heater_name):
        mr_heater_name = list(self._heater_mapping.keys())[list(self._heater_mapping.values()).index(server_heater_name)]
        return mr_heater_name

    def all_mr_heaters(self):
         return self._heater_mapping.keys()

    def get_sentry(self):
        sentryClient = raven.Client(
            'https://89fc4cf9318d46b1bfadc03c9d34577c@sentry.obico.io/8',  # noqa
            release=VERSION,
            ignore_exceptions=[]
        ) if self.sentry_opt == 'in' and self.server.canonical_endpoint_prefix().endswith('obico.io') else None
        sentry = SentryWrapper(sentryClient)
        sentry.tags_context(get_tags())
        return sentry
