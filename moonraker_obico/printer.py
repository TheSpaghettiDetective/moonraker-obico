import math
from typing import Optional, Dict, Any
import threading
import time
import pathlib

from .config import Config
from .version import VERSION
from .utils import  sanitize_filename

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

    def has_active_job(self) -> bool:
        return PrinterState.get_state_from_status(self.status) in PrinterState.ACTIVE_STATES

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
        self, print_event: Optional[str] = None, with_config: Optional[bool] = False, status_operations = None
    ) -> Dict:
        with self._mutex:
            data = {
                'current_print_ts': self.current_print_ts,
                'status': self.to_status(status_operations),
            } if self.current_print_ts is not None else {}      # Print status is un-deterministic when current_print_ts is None

            if print_event:
                data['event'] = {'event_type': print_event}

            if with_config:
                config = self.app_config
                data["settings"] = dict(
                    webcam=dict(
                        flipV=config.webcam.flip_v,
                        flipH=config.webcam.flip_h,
                        rotation=config.webcam.rotation,
                        streamRatio="16:9" if config.webcam.aspect_ratio_169 else "4:3",
                    ),
                    temperature=dict(dict(profiles=self.thermal_presets)),
                    agent=dict(
                        name="moonraker_obico",
                        version=VERSION,
                    ),
                )
            return data

    def to_status(self, status_operations) -> Dict:
        with self._mutex:
            state = self.get_state_from_status(self.status)

            if self.transient_state is not None:
                state = self.transient_state

            print_stats = self.status.get('print_stats') or dict()
            virtual_sdcard = self.status.get('virtual_sdcard') or dict()
            has_error = self.status.get('print_stats', {}).get('state', '') == 'error'
            fan = self.status.get('fan') or dict()
            gcode_move = self.status.get('gcode_move') or dict()
            print_info = print_stats.get('info') or dict()
            calculation_dict = status_operations.create_calculation_dict(print_stats, virtual_sdcard, print_info, gcode_move)

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

            completion = self.status.get('virtual_sdcard', {}).get('progress')
            print_time = print_stats.get('total_duration')
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
                    'completion': completion * 100,
                    'filepos': virtual_sdcard.get('file_position', 0),
                    'printTime': print_time,
                    'printTimeLeft': calculation_dict.get('print_time_left'),
                    'filamentUsed': print_stats.get('filament_used')
                },
                'temperatures': temps,
                'file_metadata': {
                    'analysis': {
                        'printingArea': {
                            'maxZ': calculation_dict.get('max_z')
                        }
                    },
                    'obico': {
                        'totalLayerCount': calculation_dict.get('total_layers')
                    }
                },
                'currentLayerHeight': calculation_dict.get('current_layer'),
                'currentFeedRate': gcode_move.get('speed_factor'),
                'currentFlowRate': gcode_move.get('extrude_factor'),
                'currentFanSpeed': fan.get('speed'),
                'currentZ': calculation_dict.get('current_z')
            }

def get_current_layer(print_info, file_metadata, print_stats, gcode_move, total_layers):
    if print_info.get('current_layer') is not None: 
        return print_info.get('current_layer')

    if print_stats.get('print_duration') > 0 and file_metadata.get('first_layer_height') is not None and file_metadata.get('layer_height') is not None:
        gcode_position_z = gcode_move.get('gcode_position', [])[2] if len(gcode_move.get('gcode_position', [])) > 2 else None
        if gcode_position_z is None: return None

        current_layer = math.ceil((gcode_position_z - file_metadata.get('first_layer_height')) / file_metadata.get('layer_height') + 1)
        if current_layer > total_layers: return total_layers
        if current_layer > 0: return current_layer

    return None