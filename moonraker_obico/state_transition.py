import concurrent.futures
import time
import logging
import threading

from .printer import PrinterState


_logger = logging.getLogger('obico.state_transition')

def call_func_with_state_transition(server_conn, printer_state, transient_state, func, timeout=5*60):

    def call_it():

        with concurrent.futures.ThreadPoolExecutor() as executor:
            state_before_transient = PrinterState.get_state_from_status(printer_state.status)
            printer_state.set_transient_state(transient_state)
            _logger.debug(f'Transient state started: {state_before_transient} -> {transient_state}')
            server_conn.post_status_update_to_server()

            future = executor.submit(func)
            try:
                result = future.result(timeout)
                for i in range(30):  # Wait for up to 30s for the underlining change to happen to avoid race condition.
                    if state_before_transient != PrinterState.get_state_from_status(printer_state.status):
                        return
                    time.sleep(1)
            except concurrent.futures.TimeoutError:
                _logger.warning(f'Timed out - printer_state: {printer_state} - func {func}')
            finally:
                _logger.debug(f'Transient state ended')
                printer_state.set_transient_state(None)
                server_conn.post_status_update_to_server()

    thread = threading.Thread(
        target=call_it
    )
    thread.daemon = True
    thread.start()
