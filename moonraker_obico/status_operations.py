import math


class StatusOperations:

    def __init__(self, moonrakerconn):
        self.moonrakerconn = moonrakerconn

    def get_current_layer(self, print_info, file_metadata, print_stats, gcode_move, total_layers):
        if print_info.get('current_layer') is not None: 
            return print_info.get('current_layer')

        if print_stats.get('print_duration') > 0 and file_metadata.get('first_layer_height') is not None and file_metadata.get('layer_height') is not None:
            gcode_position_z = gcode_move.get('gcode_position', [])[2] if len(gcode_move.get('gcode_position', [])) > 2 else None
            if gcode_position_z is None: return None

            current_layer = math.ceil((gcode_position_z - file_metadata.get('first_layer_height')) / file_metadata.get('layer_height') + 1)
            if current_layer > total_layers: return total_layers
            if current_layer > 0: return current_layer

        return None

    def get_total_layers(self, print_info, file_metadata):
        if print_info.get('total_layer') is not None: 
            return print_info.get('total_layer')
        
        if file_metadata.get('layer_count') is not None: 
            return file_metadata.get('layer_count')

        if file_metadata.get('first_layer_height') is not None and file_metadata.get('layer_height') is not None and file_metadata.get('object_height') is not None:
            max = math.ceil((file_metadata.get('object_height') - file_metadata.get('first_layer_height') / file_metadata.get('layer_height') + 1))
            return max if max > 0 else 0

        return None

    def get_estimated_time_avg(self, file_metadata, print_stats, virtual_sdcard):
            time = 0
            timeCount = 0
            est_time_file = self.get_est_time_file(print_stats, file_metadata, virtual_sdcard)
            est_time_filament = self.get_est_time_filament(print_stats, file_metadata)
            if est_time_file and est_time_file > 0:
                time += est_time_file
                timeCount+= 1
            if est_time_filament and est_time_filament > 0: 
                time += est_time_filament
                timeCount+= 1

            if time and timeCount: return time / timeCount

            return 0

    def get_print_percent_by_file_position_relative(self, file_metadata, virtual_sdcard):
            if file_metadata.get('filename') and file_metadata.get('gcode_start_byte') and file_metadata.get('gcode_end_byte'):
                if virtual_sdcard.get('file_position') <= file_metadata.get('gcode_start_byte'): return 0
                if virtual_sdcard.get('file_position') >= file_metadata.get('gcode_end_byte'): return 1

                currentPosition = virtual_sdcard.get('file_position') - file_metadata.get('gcode_start_byte')
                maxPosition = file_metadata.get('gcode_end_byte') - file_metadata.get('gcode_start_byte')

                if currentPosition > 0 and maxPosition > 0: return (1 / maxPosition) * currentPosition

            return virtual_sdcard.get('progress') if virtual_sdcard.get('progress') else 0

    def get_est_time_file(self, print_stats, file_metadata, virtual_sdcard):
            print_percent = self.get_print_percent_by_file_position_relative(file_metadata, virtual_sdcard)
            if print_stats and print_stats.get('print_duration') and print_stats.get('print_duration', 0) > 0 and print_percent > 0:
                return round(print_stats.get('print_duration') / print_percent - print_stats.get('print_duration'))
            return 0

    def get_est_time_filament(self, print_stats, file_metadata):
            if print_stats and print_stats.get('print_duration') and print_stats.get('filament_used') and print_stats.get('filename') and file_metadata.get('filament_total') and print_stats.get('print_duration', 0) > 0 and file_metadata.get('filament_total', 0) > 0 and file_metadata.get('filament_total', 0) > print_stats.get('filament_used', 0):
                return round(print_stats.get('print_duration') / (print_stats.get('filament_used') / file_metadata.get('filament_total')) - print_stats.get('print_duration'))
            return 0

    def get_slicer_print_time_left(self, print_stats, file_metadata):
        if print_stats and print_stats.get('print_duration') and print_stats.get('filename') and file_metadata.get('estimated_time') and print_stats.get('print_duration') > 0 and file_metadata.get('estimated_time') > 0:
            return round(file_metadata.get('estimated_time') - print_stats.get('print_duration'))
        return 0
    
    def create_calculation_dict(self, print_stats, virtual_sdcard, print_info, gcode_move):
        max_z = None
        file_metadata = None
        total_layers = None
        current_layer = None
        print_time_left = None
        slicer_print_time_left = None
        filepath = print_stats.get('filename')
        current_z = gcode_move.get('gcode_position', [])[2] if len(gcode_move.get('gcode_position', [])) > 2 else None

        if self.moonrakerconn and filepath:
            file_metadata = self.moonrakerconn.api_get('server/files/metadata', raise_for_status=True, filename=filepath)
            max_z = file_metadata.get('object_height') if file_metadata.get('object_height') else None
            total_layers = self.get_total_layers(print_info, file_metadata)
            current_layer = self.get_current_layer(print_info, file_metadata, print_stats, gcode_move, total_layers)
            print_time_left = self.get_estimated_time_avg(file_metadata, print_stats, virtual_sdcard)
            slicer_print_time_left = self.get_slicer_print_time_left(print_stats, file_metadata)
            if max_z and current_z > max_z: current_z = 0 # prevent buggy looking flicker on print start
            if not current_layer or not total_layers: # edge case handling - if either is not available we show nothing
                current_layer = None
                total_layers = None

        return {
            'maz_z': max_z,
            'current_z': current_z,
            'total_layers': total_layers,
            'current_layer': current_layer,
            'print_time_left': print_time_left,
            'slicer_print_time_left': slicer_print_time_left
        }