import logging

from py_app_runner.http_exception import HTTPException

# from memory_profiler import profile
from py_app_runner.request_handler.handlers import WebHandlerBase


# * ApiHandler
class ApiHandler(WebHandlerBase):
    """Http request handler"""

    def initialize(self) -> None:  # type: ignore
        # Override default logger
        loggerName = f"{__name__}.{self.__class__.__name__}"
        self.logger = logging.getLogger(loggerName)

    ########################
    ### Request Handling ###
    ########################
    # def set_default_headers(self):
    #     # self.set_header("Access-Control-Allow-Origin", "*")
    #     origin = self.request.headers.get(  # type: ignore
    #         "Origin", "*"
    #     )  # use current requesting origin
    #     self.set_header("Access-Control-Allow-Origin", origin)  # type: ignore
    #     self.set_header(
    #         "Access-Control-Allow-Headers",
    #         "X-API-Key,Authorization,Accept,Origin,DNT,X-CustomHeader,Keep-Alive,User-Agent,X-Requested-With,
    #           If-Modified-Since,Cache-Control,Content-Type,Content-Range,Range",
    #     )
    #     self.set_header("Access-Control-Allow-Methods", "POST,GET,OPTIONS")
    #     self.set_header("Access-Control-Max-Age", 10800)
    #     self.set_header("Access-Control-Request-Headers", "*")
    #     self.set_header("Access-Control-Allow-Credentials", "true")

    async def options(self, service: str | None = None, action: str | None = None) -> None:  # type: ignore
        self.set_header("Content-Type", "text/plain charset=UTF-8")
        self.set_header("Content-Length", 0)
        self.set_status(204)
        await self.finish()  # type: ignore

    # @profile
    async def get(self, service: str | None = None, action: str | None = None) -> None:  # type: ignore
        """Get request"""
        try:
            async with self.timer.aenter("bridge.get"):
                if self.version == 1:
                    await self.get_v1(service=service, action=action)
                else:
                    raise HTTPException("Invalid version", code=1001, http_status=400)

        except HTTPException as e:
            self.log_request(error=e)
            self.error(str(e.message), e.code or -1, e.http_status)

        except Exception as e:
            self.log_request(error=e)
            self.logger.exception(f"Error processing request with exception: {e}")
            self.error(
                "Found an error while processing your request. Please try again later."
                " Send us a message if the problem persists."
            )

        finally:
            self.authToken = None

            # Print debug and free resources
            self.timer.print_timer_stats()
            self.timer.reset_timers()

    async def post(self, service: str | None = None, action: str | None = None) -> None:  # type: ignore[override]
        """Post request is just a forward to get request"""
        await self.get(service, action)

    ##########
    ### V1 ###
    ##########
    async def get_v1(self, service: str | None = None, action: str | None = None) -> None:
        """
        Requests always have to be in this format:
        {
            "action": "action_name",
            "data": {
                "input1": "value1",
                "input2": "value2",
                ...
            }
        }
        """

        async with self.timer.aenter("bridge.get_v1.findService"):
            # Move to finding a service
            res = self.find_service(service_name=service)
            if not res.result:
                raise HTTPException(
                    f"Could not find service by provided name: {service}",
                    code=1002,
                    http_status=400,
                )

            # Bridge request handler
            bridge_request, requires_api_key = res.result
            if not bridge_request:
                raise HTTPException(f"Service not found: {service}", code=1003, http_status=400)

        # Timer
        async with self.timer.aenter("bridge.get_v1.withPools"):
            # Check for valid api key
            if requires_api_key is not False:
                status = await self.has_valid_api_key()
                if isinstance(status, str):
                    raise HTTPException(
                        f"Application is not authenticated: {status}",
                        code=1401,
                        http_status=401,
                    )

        # Timer
        async with self.timer.aenter("bridge.get_v1.run_request_handler"):
            # Find request data
            request_data = self.get_request_data(action)
            action = request_data.get("action", None)
            input_data = request_data.get("data", {})

            if not action:
                raise HTTPException("Missing action", code=1005, http_status=400)

            return_data = await bridge_request(action, input_data, self)
            if return_data is not None:
                self.write(return_data)
                self.log_request(response=return_data, level="debug")
            else:
                raise HTTPException("No return data from service", code=1006, http_status=400)
