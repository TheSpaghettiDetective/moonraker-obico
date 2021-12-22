Standalone application for TheSpaghettiDetective - moonraker integration
------------------------------------------------------------------------

Alpha version, with limited functionality and for development only.

Please don't use it in production and be careful while using it!


Preparing Moonraker
-------------------

Following sections are mandatory in moonraker config file:

```
[virtual_sdcard]
path: <path to existing dir>

[display_status]

[pause_resume]

[history]

```

Grab moonraker api key from ```/access/api_key```.


config.ini
----------

```
[thespaghettidetective]
url = <by default it connects to TSD Cloud>
auth_token = <filled in by link command, see bellow>

[moonraker]
url = <url for moonraker api, default is http://127.0.0.1:7125>
api_key = <grab it from moonraker, visit /access/api_key from trusted host>

[webcam]
snapshot_url = <defaults to http://127.0.0.1:8080/?action=snapshot>
# or
# stream_url = http://127.0.0.1:8080/?action=stream
```


How to run
----------

    # requires python3; install python3 packages

    sudo apt-get install python3 python3-pip python3-venv

    # clone repo

    git clone https://github.com/TheSpaghettiDetective/tsd-moonraker.git

    # setup virtual environment

    python3 -m venv tsd-moonraker/
    cd tsd-moonraker
    source venv/bin/activate
    pip3 install -r requirements.txt

    # fill in essential configuration

    cp config.sample.ini config.ini
    nano config.ini

    # link printer (grab tsd auth token)

    python3 -m tsd_moonraker.link -c config.ini

    # start app

    python3 -m tsd_moonraker.app -c config.ini -l tsd_moonraker.log
