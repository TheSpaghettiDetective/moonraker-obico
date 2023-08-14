class Celestrius:
    def __init__(self, config):
        self.server = config.get_server()
        self.name = config.get_name()

        # Raises an error if "example_int_option" is not configured in
        # the [example] section
        self.example_int_opt = config.getint("example_int_option")

        # Returns a NoneType if "example_float_option is not configured
        # in the config
        self.example_float_opt = config.getfloat("example_float_option", None)

        self.server.register_endpoint("/server/example", ['GET'],
                                      self._handle_example_request)

    async def request_some_klippy_state(self):
        klippy_apis = self.server.lookup_component('klippy_apis')
        return await klippy_apis.query_objects({'print_stats': None})

    async def _handle_example_request(self, web_request):
        web_request.get_int("required_reqest_param")
        web_request.get_float("optional_request_param", None)
        state = await self.request_some_klippy_state()
        return {"example_return_value": state}

def load_component(config):
    return Celestrius(config)