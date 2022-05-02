import argparse
import logging
import requests
import signal
import sys

from .utils import raise_for_status
from .config import Config

logging.basicConfig()


if __name__ == '__main__':

    def linking_interupted(signum, frame):
        print("""

The process to link to the Obico Server is interrupted.
To resume the linking process at a later time, run:

~/moonraker-obico/install.sh

""")
        sys.exit(1)


    signal.signal(signal.SIGINT, linking_interupted)

    parser = argparse.ArgumentParser()
    parser.add_argument(
        '-c', '--config', dest='config_path', required=True,
        help='Path to config file (ini)'
    )
    args = parser.parse_args()
    config = Config.load_from(args.config_path)

    if config.server.auth_token:
        print("""
WARNING: Authentication token found! Proceed only if you want to re-link your printer to the Obico server.
""")

    endpoint_prefix = config.server.canonical_endpoint_prefix()
    print(
        f'Visit\n\n    {endpoint_prefix}/printers/\n\n'
        'or the mobile app and start linking '
        'a printer and switch to Manual Setup mode!\n\n'
        'You need to find a verification code and paste it bellow.'
    )

    url = f'{config.server.url}/api/v1/octo/verify/'
    while True:
        code = input('\nEnter verification code (or leave it empty to quit): ')
        if not code.strip():
            break

        try:
            resp = requests.post(url, params={'code': code.strip()})
            raise_for_status(resp, with_content=True)
            data = resp.json()
            auth_token = data['printer']['auth_token']
            print(f'Got auth token "{auth_token}".')
            config.update_tsd_auth_token(auth_token)
            print('Updated config.')
            break
        except Exception:
            logging.exception('Something went wrong.')
