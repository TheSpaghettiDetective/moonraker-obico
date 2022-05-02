import argparse
import logging
import requests

from .utils import raise_for_status
from .config import Config

logging.basicConfig()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument(
        '-c', '--config', dest='config_path', required=True,
        help='Path to config file (ini)'
    )
    args = parser.parse_args()
    config = Config.load_from(args.config_path)

    print('Hi!')

    if config.server.auth_token:
        print('\nWARNING: Current tsd authentication token '
              'is going to be overwritten!\n')

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
