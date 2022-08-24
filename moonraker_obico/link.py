import argparse
import logging
import requests
import signal
import sys

from .utils import raise_for_status
from .config import Config

logging.basicConfig()


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
    args = parser.parse_args()
    config = Config(args.config_path)

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
            linking_interrupted(None, None)

        try:
            resp = requests.post(url, params={'code': code.strip()})
            raise_for_status(resp, with_content=True)
            data = resp.json()
            auth_token = data['printer']['auth_token']
            config.update_server_auth_token(auth_token)
            print('\n###### Successfully linked to your Obico Server account!')
            break
        except Exception:
            print(RED + '\n==== Failed to link. Did you enter an expired code? ====\n' + NC)

