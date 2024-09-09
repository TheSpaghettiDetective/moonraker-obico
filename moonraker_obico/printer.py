import math
import platform
from typing import Optional, Dict, Any
import threading
import time
import pathlib

from .config import Config
from .version import VERSION
from .utils import sanitize_filename

class PrinterState:
    STATE_OFFLINE = 'Offline'
    STATE_OPERATIONAL = 'Operational'
    STATE_GCODE_DOWNLOADING = 'G-Code Downloading'
    STATE_PRINTING = 'Printing'
    STATE_PAUSING = 'Pausing'
    STATE_PAUSED = 'Paused'
    STATE_RESUMING = 'Resuming'
    STATE_CANCELLING = 'Cancelling'

    EVENT_STARTED = 'PrintStarted'
    EVENT_RESUMED = 'PrintResumed'
    EVENT_PAUSED = 'PrintPaused'
    EVENT_CANCELLED = 'PrintCancelled'
    EVENT_DONE = 'PrintDone'
    EVENT_FAILED = 'PrintFailed'

    ACTIVE_STATES = [STATE_PRINTING, STATE_PAUSED]

    def __init__(self, app_config: Config):
        self._mutex = threading.RLock()
        self.app_config = app_config
        self.status = {}
        self.current_print_ts = None
        self.obico_g_code_file_id = None
        self.transient_state = None
        self.thermal_presets = []
        self.installed_plugins = []
        self.current_file_metadata = None
        self.webcams = None
        self.data_channel_id = None

    def has_active_job(self) -> bool:
        return PrinterState.get_state_from_status(self.status) in PrinterState.ACTIVE_STATES

    def is_busy(self) -> bool:
        with self._mutex:
            return self.status.get('print_stats', {}).get('state') in ['printing', 'paused']

    def is_printing(self) -> bool:
        with self._mutex:
            return self.status.get('print_stats', {}).get('state') == 'printing'

    # Return: The old status.
    def update_status(self, new_status: Dict) -> Dict:
        with self._mutex:
            old_status = self.status
            self.status = new_status
        return old_status

    # Return: The old current_print_ts.
    def set_current_print_ts(self, new_current_print_ts):
        with self._mutex:
            old_current_print_ts = self.current_print_ts
            self.current_print_ts = new_current_print_ts
            if self.current_print_ts == -1:
                self.set_obico_g_code_file_id(None)

        return old_current_print_ts

    def set_obico_g_code_file_id(self, obico_g_code_file_id):
        with self._mutex:
            self.obico_g_code_file_id = obico_g_code_file_id

    def set_transient_state(self, transient_state):
        with self._mutex:
            self.transient_state = transient_state

    def get_obico_g_code_file_id(self):
        with self._mutex:
            return self.obico_g_code_file_id

    def set_webcams(self, webcams, data_channel_id):
        with self._mutex:
            self.webcams = webcams
            self.data_channel_id = data_channel_id

    @classmethod
    def get_state_from_status(cls, data: Dict) -> str:
        klippy_state = data.get(
            'webhooks', {}
        ).get('state', 'disconnected')

        # TODO: We need to have better understanding on the webhooks.state.
        if klippy_state != 'ready':
            return PrinterState.STATE_OFFLINE

        return {
            'standby': PrinterState.STATE_OPERATIONAL,
            'printing': PrinterState.STATE_PRINTING,
            'paused': PrinterState.STATE_PAUSED,
            'complete': PrinterState.STATE_OPERATIONAL,
            'cancelled': PrinterState.STATE_OPERATIONAL,
            'error': PrinterState.STATE_OPERATIONAL, # state is "error" when printer quits a print due to an error, but operational
        }.get(data.get('print_stats', {}).get('state', 'unknown'), PrinterState.STATE_OFFLINE)

    def to_dict(
        self, print_event: Optional[str] = None, with_settings: Optional[bool] = False
    ) -> Dict:
        with self._mutex:
            data = {
                'current_print_ts': self.current_print_ts,
                'status': self.to_status(),
            } if self.current_print_ts is not None else {}      # Print status is un-deterministic when current_print_ts is None

            if print_event:
                data['event'] = {'event_type': print_event}

            if with_settings:
                data["settings"] = dict(
                    webcams=self.webcams,
                    data_channel_id=self.data_channel_id,
                    temperature=dict(dict(profiles=self.thermal_presets)),
                    agent=dict(
                        name="moonraker_obico",
                        version=VERSION,
                    ),
                    platform_uname=list(platform.uname()),
                    installed_plugins=self.installed_plugins,
                )
                try:
                    with open('/proc/device-tree/model', 'r') as file:
                        model = file.read().strip()
                    data['settings']['platform_uname'].append(model)
                except:
                    data['settings']['platform_uname'].append('')
            return data

    def to_status(self) -> Dict:
        with self._mutex:
            state = self.get_state_from_status(self.status)

            if self.transient_state is not None:
                state = self.transient_state

            print_stats = self.status.get('print_stats') or dict()
            virtual_sdcard = self.status.get('virtual_sdcard') or dict()
            has_error = self.status.get('print_stats', {}).get('state', '') == 'error'
            fan = self.status.get('fan') or dict()
            gcode_move = self.status.get('gcode_move') or dict()

            temps = {}
            for heater in self.app_config.all_mr_heaters():
                data = self.status.get(heater, {})

                temps[self.app_config.get_mapped_server_heater_name(heater)] = {
                    'actual': round(data.get('temperature', 0.), 2),
                    'offset': 0,
                    'target': data.get('target'), # "target = null" indicates this is a sensor, not a heater, and hence temperature can't be set
                }

            filepath = print_stats.get('filename')
            filename = pathlib.Path(filepath).name if filepath else None
            file_display_name = sanitize_filename(filename) if filename else None

            if state == PrinterState.STATE_OFFLINE:
                return {}

            completion, print_time, print_time_left = self.get_time_info()
            current_z, max_z, total_layers, current_layer = self.get_z_info()

            return {
                '_ts': time.time(),
                'state': {
                    'text': state,
                    'flags': {
                        'operational': state not in [PrinterState.STATE_OFFLINE, PrinterState.STATE_GCODE_DOWNLOADING],
                        'paused': state == PrinterState.STATE_PAUSED,
                        'printing': state == PrinterState.STATE_PRINTING,
                        'cancelling': False,
                        'pausing': False,
                        'error': has_error,
                        'ready': state == PrinterState.STATE_OPERATIONAL,
                        'closedOrError': False,  # OctoPrint uses this flag to indicate the printer is connectable. It should always be false until we support connecting moonraker to printer
                    },
                    'error': print_stats.get('message') if has_error else None
                },
                'currentZ': None,
                'job': {
                    'file': {
                        'name': filename,
                        'path': filepath,
                        'display': file_display_name,
                        'obico_g_code_file_id': self.get_obico_g_code_file_id(),
                    },
                    'estimatedPrintTime': None,
                    'user': None,
                },
                'progress': {
                    'completion': completion * 100 if completion is not None else None,
                    'filepos': virtual_sdcard.get('file_position', 0),
                    'printTime': print_time if print_time is not None else None,
                    'printTimeLeft': print_time_left if print_time_left is not None else None,
                    'filamentUsed': print_stats.get('filament_used')
                },
                'temperatures': temps,
                'file_metadata': {
                    'analysis': {
                        'printingArea': {
                            'maxZ': max_z
                        }
                    },
                    'obico': {
                        'totalLayerCount': total_layers
                    }
                },
                'currentLayerHeight': current_layer,
                'currentFeedRate': gcode_move.get('speed_factor'),
                'currentFlowRate': gcode_move.get('extrude_factor'),
                'currentFanSpeed': fan.get('speed'),
                'currentZ': current_z
            }

    def get_z_info(self):
        '''
        return: (current_z, max_z, current_layer, total_layers). Any of them can be None
        '''
        print_stats = self.status.get('print_stats') or dict()
        print_info = print_stats.get('info') or dict()
        file_metadata = self.current_file_metadata
        is_not_busy = self.is_busy() is False or self.transient_state is not None
        has_print_duration = print_stats.get('print_duration', 0) > 0

        current_z = None
        max_z = None
        total_layers = print_info.get('total_layer')
        current_layer = print_info.get('current_layer')

        if not current_layer:
            first_layer_macro_status = self.status.get('gcode_macro _OBICO_LAYER_CHANGE', {})
            if first_layer_macro_status.get('current_layer', -1) > 0: # current_layer > 0 means macros is embedded in gcode
                current_layer = first_layer_macro_status['current_layer']

        gcode_position = self.status.get('gcode_move', {}).get('gcode_position', [])
        current_z = gcode_position[2] if len(gcode_position) > 2 else None

        # Credit: https://github.com/mainsail-crew/mainsail/blob/develop/src/store/printer/getters.ts#L122

        if file_metadata:
            max_z = file_metadata.get('object_height')

            if total_layers is None:
                total_layers = file_metadata.get('layer_count')

            first_layer_height = file_metadata.get('first_layer_height')
            layer_height = file_metadata.get('layer_height')
            layer_heights_in_metadata = layer_height is not None and first_layer_height is not None

            if total_layers is None and layer_heights_in_metadata and max_z:
                total_layers = math.ceil(((max_z - first_layer_height) / layer_height + 1))
                total_layers = max(total_layers, 0) # Apparently the previous calculation can result in negative number in some cases...

            if current_layer is None and layer_heights_in_metadata and current_z and total_layers:
                current_layer = math.ceil((current_z - first_layer_height) / layer_height + 1)
                current_layer = min(total_layers, current_layer) # Apparently the previous calculation can result in current_layer > total_layers in some cases...
                current_layer = max(current_layer, 0) # Apparently the previous calculation can result in negative number in some cases...

        if max_z and current_z > max_z: current_z = 0 # prevent buggy looking flicker on print start
        if current_layer is None or total_layers is None or is_not_busy or not has_print_duration: # edge case handling - if either are not available we show nothing / show nothing if paused state, transient, etc / show nothing if no print duration (prevents tracking z height during preheat & start bytes)
            current_layer = None
            total_layers = None

        return (current_z, max_z, total_layers, current_layer)

    def get_time_info(self):
        print_stats = self.status.get('print_stats') or dict()
        completion = self.status.get('virtual_sdcard', {}).get('progress')
        print_time = print_stats.get('total_duration')
        actual_print_duration = print_stats.get('print_duration')
        estimated_time = actual_print_duration / completion if actual_print_duration is not None and completion is not None and completion > 0.001 else None
        print_time_left = estimated_time - actual_print_duration if estimated_time is not None and actual_print_duration is not None else None

        file_metadata = self.current_file_metadata
        if file_metadata and file_metadata.get('estimated_time'):
            slicer_time_left = file_metadata.get('estimated_time') - actual_print_duration if actual_print_duration is not None else 1
            print_time_left = slicer_time_left if slicer_time_left > 0 else 1

        return (completion, print_time, print_time_left)
