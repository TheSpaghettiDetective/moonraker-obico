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
        print("""

   ____
  / __ \\
 | |  | | ___   ___   ___   ___  _ __  ___
 | |  | |/ _ \\ / _ \\ / _ \\ / _ \\| '_ \\/ __|
 | |__| | (_) | (_) | (_) | (_) | |_) \\__ \\  _   _   _
  \\____/ \\___/ \\___/ \\___/ \\___/| .__/|___/ (_) (_) (_)
                                | |
                                |_|

"""+RED+"""

The process to link to your Obico Server account was interrupted.

"""+NC+"""
To resume the linking process at a later time, run:

-------------------------------------------------------------------------------------------------
cd ~/moonraker-obico
./install.sh
-------------------------------------------------------------------------------------------------

Need help? Stop by:

- The Obico's help docs: https://obico.io/help/
- The Obico community: https://obico.io/discord/

""")
        sys.exit(1)


    signal.signal(signal.SIGINT, linking_interrupted)

    parser = argparse.ArgumentParser()
    parser.add_argument(
        '-c', '--config', dest='config_path', required=True,
        help='Path to config file (ini)'
    )
    args = parser.parse_args()
    config = Config.load_from(args.config_path)

    if config.server.auth_token:
        print(RED+"""
!!!WARNING: Authentication token found! Proceed only if you want to re-link your printer to the Obico server.
For more information, visit: https://obico.io/docs/user-guides/relink-klipper

"""+NC)

    endpoint_prefix = config.server.canonical_endpoint_prefix()
    print("""
To link to your Obico Server account, you need to obtain the 6-digit verification code
in the Obico mobile or web app, and enter the code below.

If you need help, head to https://obico.io/docs/user-guides/klipper-setup
"""
    )

    url = f'{config.server.url}/api/v1/octo/verify/'
    while True:
        code = input('\nEnter verification code (or leave it empty to quit): ')
        if not code.strip():
            linking_interrupted(None, None)

        try:
            resp = requests.post(url, params={'code': code.strip()})
            raise_for_status(resp, with_content=True)
            data = resp.json()
            auth_token = data['printer']['auth_token']
            config.update_tsd_auth_token(auth_token)
            print('\n###### Sccuessfully linked to your Obico Server account!')
            break
        except Exception:
            print(RED + '\n==== Failed to link. Did you enter an expired code? ====\n' + NC)

