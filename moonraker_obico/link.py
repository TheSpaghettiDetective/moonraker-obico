import argparse
import logging
import requests
import signal
import sys
import os
import time
import select

from .utils import raise_for_status, run_in_thread, verify_link_code, SentryWrapper
from .config import Config
from .printer_discovery import PrinterDiscovery

logging.basicConfig()


CYAN='\033[0;96m'
RED='\033[0;31m'
NC='\033[0m' # No Color

if __name__ == '__main__':

    def linking_interrupted(signum, frame):
        print("\033[?25h")
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

    if config.server.auth_token:
        print(RED+"""
!!!WARNING: Moonraker-obico already linked!
Proceed only if you want to re-link your printer to the Obico server.
For more information, visit:
https://www.obico.io/docs/user-guides/relink-klipper
"""+NC)
        confirmed = input('\nAre you sure you want to continue? [y/N] ').strip()
        if confirmed not in ('Y', 'y'):
            sys.exit(255)

        config._config.remove_option('server', 'auth_token')
        config.write()

    discovery = PrinterDiscovery(config, sentry)
    discoverable = True

    def spin():
        global discoverable

        sys.stdout.write("\033[?25l") # Hide cursor
        sys.stdout.flush()

        spinner = ["|", "/", "-", "\\"]
        spinner_idx = 0

        while discoverable:
            sys.stdout.write("Scanning the local network  " + spinner[spinner_idx] + "\r")
            sys.stdout.flush()
            spinner_idx = (spinner_idx + 1) % 4

            rlist, _, _ = select.select([sys.stdin], [], [], 0.1)  # Poll with a timeout of 0.1 seconds
            if sys.stdin in rlist:
                sys.stdin.readline()
                break

        sys.stdout.write("\033[?25h") # Show cursor
        sys.stdout.flush()

    def run_discovery():
        global discoverable
        global discovery
        try:
            discovery.start_and_block(300) # waiting for 2*300 seconds = 10 minutes
        finally:
            discoverable = False

    def wait_for_one_time_passcode(timeout=10):
        global discovery
        for _ in range(int(timeout / 0.1)):
            one_time_passcode = discovery.get_one_time_passcode()
            if one_time_passcode:
                break

            time.sleep(0.1)
        return one_time_passcode


    logging.getLogger('werkzeug').setLevel(logging.ERROR)
    discovery_thread = run_in_thread(run_discovery)

    one_time_passcode = wait_for_one_time_passcode(timeout=5)

    print("""
Now open the Obico mobile or web app. If your phone or computer is connected to the
same network as your printer, you will see this printer listed in the app. Click
"Link Now" and you will be all set!

If you need help, head to https://www.obico.io/docs/user-guides/klipper-setup

Your printer is now discoverable by the Obico app on the same network.""")
    if one_time_passcode:
        print(f"""If you can't find the printer in the app, switch to manual linking and enter:  {CYAN}{one_time_passcode}{NC}
        """)
        print(f"""
If you are using a Obico app version older than 2.0, press 'Enter' to switch to using 6-digit verification code.
        """)

    while discoverable:
        spin()
        if discoverable:
            confirmed = input('\nSwitch to using 6-digit verification code to link printer? [Y/n] ').strip()
            if confirmed not in ('N', 'n'):
                discoverable = False
            else:
                print('Continue waiting...')

    discovery.stop()
    discovery_thread.join()

    config.load_from_config_file() # PrinterDiscovery may or may not have succeeded. Reload from the file to make sure auth_token is loaded
    if config.server.auth_token: # linked successfully
        sys.exit(0)
    else:
        print("\n### Switched to using 6-digit verification code to link printer. ###")

    print("""
To link to your Obico Server account, you need to obtain the 6-digit verification code
in the Obico mobile or web app, and enter the code below.

If you need help, head to https://www.obico.io/docs/user-guides/klipper-setup
""")

    while True:
        code = input('\nEnter verification code (or leave it empty to abort): ')
        if not code.strip():
            sys.exit(255)

        try:
            if debug:
                print(f'## DEBUG: Verifying code "{code.strip()}" at server URL: "{config.server.canonical_endpoint_prefix()}"')

            resp = verify_link_code(config, code)

            if debug:
                print(f'## DEBUG: Server response code "{resp}"')

            resp.raise_for_status()
            print('\n###### Successfully linked to your Obico Server account!')
            break
        except Exception as e:
            if debug:
                print('## DEBUG: Server API error: ', str(e))

            print(RED + '\n==== Failed to link. Did you enter an expired code? ====\n' + NC)
            print('If you keep getting this error, press ctrl-c to abort it and then run the following command to debug:')
            print(CYAN + f'PYTHONPATH={os.environ.get("PYTHONPATH")} {os.environ.get("OBICO_ENV")}/bin/python3 -m moonraker_obico.link {" ".join(sys.argv[1:])} -d' + NC)
