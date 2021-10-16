Standalone application for moonraker integration
------------------------------------------------

Alpha version, for development only. Don't use it in production.


How to run
----------

    # pull

    git pull https://github.com/TheSpaghettiDetective/tsd-moonraker
    cd tsd-moonraker
    
    # requires python3; install python3 packages

    virtualenv venv
    source venv/bin/activate
    pip install -r requirements.txt
    
    # fill in essential configuration;
    cp config.sample.ini config.ini
    vim config.ini

    # link printer (grab tsd auth token)
    python -m tsd_moonraker.link -c config.ini

    # start app
    python -m tsd_moonraker.app -c config.ini
