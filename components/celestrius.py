class Celestrius:
    def __init__(self, config):
        self.server = config.get_server()
        self.celestrius_compatible = None,
        self.celestrius_url = None,
        self.images = []

        self.server.register_endpoint("/server/celestrius_config", ['POST'],
                                      self._load_config)
        self.server.register_endpoint("/server/reset_celestrius", ['POST'],
                                      self._reset_celestrius)
        self.server.register_endpoint("/server/celestrius_config", ['POST'],
                                      self._load_config)

    async def _load_config(self, web_request):
        self.celestrius_compatible = web_request.get_int("celestrius_compatible")
        self.celestrius_url = web_request.get_int("celestrius_url")
        return 'ok'
    
    async def _reset_celestrius(self):
        return # TODO 

    
    async def take_img(self):
        return # TODO 

    async def dump_images_to_server(self):
        return # TODO 


def load_component(config):
    return Celestrius(config)