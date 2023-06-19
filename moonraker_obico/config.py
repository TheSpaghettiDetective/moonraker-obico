import dataclasses
from typing import Optional
import re
from configparser import ConfigParser
from urllib.parse import urlparse
import logging

from .utils import SentryWrapper

_logger = logging.getLogger('obico.config')

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
class TunnelConfig:
    dest_host: Optional[str]
    dest_port: Optional[str]
    dest_is_ssl: Optional[str]
    url_blacklist: []


@dataclasses.dataclass
class WebcamConfig:

    def __init__(self, webcam_config_section):
        self.webcam_config_section = webcam_config_section
        self.moonraker_webcam_config = {}

    @property
    def snapshot_url(self):
        return self.webcam_full_url(self.webcam_config_section.get('snapshot_url') or self.moonraker_webcam_config.get('snapshot_url'))

    @property
    def disable_video_streaming(self):
        try:
            return self.webcam_config_section.getboolean('disable_video_streaming', False)
        except:
            _logger.warn(f'Invalid disable_video_streaming value. Using default.')
            return False

    @property
    def target_fps(self):
        try:
            fps = float( self.webcam_config_section.get('target_fps') or self.moonraker_webcam_config.get('target_fps') )
        except:
            fps = 25
        return min(fps, 25)

    @property
    def snapshot_ssl_validation(self):
        return False

    @property
    def stream_url(self):
        return self.webcam_full_url(self.webcam_config_section.get('stream_url') or self.moonraker_webcam_config.get('stream_url'))

    @property
    def flip_h(self):
        if 'flip_h' in self.webcam_config_section:
            try:
                return self.webcam_config_section.getboolean('flip_h')
            except:
                _logger.warn(f'Invalid flip_h value. Using default.')

        return self.moonraker_webcam_config.get('flip_h')

    @property
    def flip_v(self):
        if 'flip_v' in self.webcam_config_section:
            try:
                return self.webcam_config_section.getboolean('flip_v')
            except:
                _logger.warn(f'Invalid flip_v value. Using default.')

        return self.moonraker_webcam_config.get('flip_v')

    @property
    def rotation(self):
        invalid_value_message = f'Invalid rotation value. Valid values: [0, 90, 180, 270]. Using default.'
        try:
            rotation = self.webcam_config_section.getint('rotation', 0)
            if not rotation in [0, 90, 180, 270]:
                _logger.warn(invalid_value_message)
                return 0
            return rotation
        except:
            _logger.warn(invalid_value_message)
            return 0

    @property
    def aspect_ratio_169(self):
        try:
            return self.webcam_config_section.getboolean('aspect_ratio_169', False)
        except:
            _logger.warn(f'Invalid aspect_ratio_169 value. Using default.')
            return False

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

        dest_is_ssl = False
        try:
            dest_is_ssl = config.getboolean('tunnel', 'dest_is_ssl', fallback=False,)
        except:
            _logger.warn(f'Invalid dest_is_ssl value. Using default.')

        self.tunnel = TunnelConfig(
            dest_host=config.get(
                'tunnel', 'dest_host',
                fallback='127.0.0.1',
            ),
            dest_port=config.get(
                'tunnel', 'dest_port',
                fallback='80',
            ),
            dest_is_ssl=dest_is_ssl,
            url_blacklist=[],
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

        self._config = config


    def write(self) -> None:
        with open(self._config_path, 'w') as f:
            self._config.write(f)

    def update_server_auth_token(self, auth_token: str):
        self.server.auth_token = auth_token
        self._config.set('server', 'auth_token', auth_token)
        self.write()

    # Adopted from getHeaters, getTemperatureObjects, getTemperatureSensors in mainsail:/src/store/printer/getters.ts
    def update_heater_mapping(self, heaters):
        def capwords(s):
            return ' '.join(elem.capitalize() for elem in s.split(' '))

        for heater in sorted(heaters.get('available_heaters', [])):
            name = heater
            name_split = name.split(' ')
            if len(name_split) > 1 and name_split[0] == 'heater_generic':
                name = name_split[1]

            if name.startswith('_'):
                continue

            self._heater_mapping[heater] = name

        for sensor in sorted(heaters.get('available_sensors', [])):
            name_split = sensor.split(' ')
            if len(name_split) > 1 and name_split[0] == 'temperature_sensor' and not name_split[1].startswith('_'):
                self._heater_mapping[sensor] = name_split[1]


    def get_mapped_server_heater_name(self, mr_heater_name):
        return self._heater_mapping.get(mr_heater_name)

    def get_mapped_mr_heater_name(self, server_heater_name):
        mr_heater_name = list(self._heater_mapping.keys())[list(self._heater_mapping.values()).index(server_heater_name)]
        return mr_heater_name

    def all_mr_heaters(self):
         return self._heater_mapping.keys()
