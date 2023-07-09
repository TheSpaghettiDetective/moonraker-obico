import argparse
import logging
import requests
import signal
import sys
import os

from .utils import raise_for_status
from .config import Config

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
        '-d', '--debug', default=False, action="store_true",
        help='Print debugging info'
    )
    args = parser.parse_args()
    config = Config(args.config_path)
    debug = args.debug

    if config.server.auth_token:
        print(RED+"""
!!!WARNING: Moonraker-obico already linked!
Proceed only if you want to re-link your printer to the Obico server.
For more information, visit:
https://obico.io/docs/user-guides/relink-klipper

To abort, simply press 'Enter'.

"""+NC)

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

