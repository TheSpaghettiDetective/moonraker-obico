import argparse
import logging
import requests
import signal
import sys
import os
import time

from .utils import raise_for_status, run_in_thread, verify_link_code, SentryWrapper
from .config import Config
from .printer_discovery import PrinterDiscovery

logging.basicConfig()


CYAN='\033[0;96m'
RED='\033[0;31m'
NC='\033[0m' # No Color

if __name__ == '__main__':

    def linking_interrupted(signum, frame):
        print('')
        sys.exit(1)

    signal.signal(signal.SIGINT, linking_interrupted)

    parser = argparse.ArgumentParser()
    parser.add_argument(
        '-c', '--config', dest='config_path', required=True,
        help='Path to config file (ini)'
    )
    parser.add_argument(
        '-d', '--debug', dest='debug', default=False, action="store_true",
        help='Print debugging info'
    )
    params = parser.parse_args()
    config = Config(params.config_path)
    config.load_from_config_file()
    sentry = SentryWrapper(config=config)
    debug = params.debug

    skip_printer_discovery = False
    if config.server.auth_token:
        print(RED+"""
!!!WARNING: Moonraker-obico already linked!
Proceed only if you want to re-link your printer to the Obico server.
For more information, visit:
https://obico.io/docs/user-guides/relink-klipper

To abort, simply press 'Enter'.

"""+NC)
        skip_printer_discovery = True

    if not skip_printer_discovery:
        discoverable = True
        def spin():
            sys.stdout.write("\033[?25l") # Hide cursor
            sys.stdout.flush()

            spinner = ["|", "/", "-", "\\"]
            spinner_idx = 0

            while discoverable:
                sys.stdout.write(spinner[spinner_idx] + "\r")
                sys.stdout.flush()
                spinner_idx = (spinner_idx + 1) % 4
                time.sleep(0.1)

            sys.stdout.write("\033[?25h") # Show cursor
            sys.stdout.flush()


        print("""
Now open the Obico mobile or web app. If your phone or computer is connected to the
same network as your printer, you will see this printer listed in the app. Click
"Link Now" and you will be all set!

If you need help, head to https://obico.io/docs/user-guides/klipper-setup

Waiting for Obico app to link this printer automatically...  press 'Enter' if you
want to link your printer using a 6-digit verification code instead.
""")
        logging.getLogger('werkzeug').setLevel(logging.ERROR)
        discovery = PrinterDiscovery(config, sentry)
        discovery_thread = run_in_thread(discovery.start_and_block)
        spinner_thread = run_in_thread(spin)

        input('')

        discoverable = False
        discovery.stop()
        spinner_thread.join()
        discovery_thread.join()

        config.load_from_config_file() # PrinterDiscovery may or may not have succeeded. Reload from the file to make sure auth_token is loaded
        print("\n### Switched to using 6-digit verification code to link printer. ###")

    print("""
To link to your Obico Server account, you need to obtain the 6-digit verification code
in the Obico mobile or web app, and enter the code below.

If you need help, head to https://obico.io/docs/user-guides/klipper-setup
""")

    endpoint_prefix = config.server.canonical_endpoint_prefix()

    url = f'{config.server.url}/api/v1/octo/verify/'
    while True:
        code = input('\nEnter verification code (or leave it empty to abort): ')
        if not code.strip():
            sys.exit(255)

        try:
            if debug:
                print(f'## DEBUG: Verifying code "{code.strip()}" at server URL: "{url}"')

            resp = requests.post(url, params={'code': code.strip()})

            if debug:
                print(f'## DEBUG: Server response code "{resp}"')

            raise_for_status(resp, with_content=True)
            data = resp.json()
            auth_token = data['printer']['auth_token']
            config.update_server_auth_token(auth_token)
            print('\n###### Successfully linked to your Obico Server account!')
            break
        except Exception:
            print(RED + '\n==== Failed to link. Did you enter an expired code? ====\n' + NC)
            if not debug:
                print('If you keep getting this error, press ctrl-c to abort it and then run the following command to debug:')
                print(CYAN + f'PYTHONPATH={os.environ.get("PYTHONPATH")} {os.environ.get("OBICO_ENV")}/bin/python3 -m moonraker_obico.link {" ".join(sys.argv[1:])} -d' + NC)

