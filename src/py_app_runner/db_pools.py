from database_wrapper_pgsql import PgsqlWithPoolingAsync
from database_wrapper_redis import RedisDbWithPoolAsync


class DbPools:
    """
    Concrete class to hold database connection pools and wrappers for use
    throughout the application
    """

    cache_db_pool: RedisDbWithPoolAsync
    main_db_pool: PgsqlWithPoolingAsync

    def __init__(
        self,
        cache_db_pool: RedisDbWithPoolAsync,
        main_db_pool: PgsqlWithPoolingAsync,
    ):
        self.cache_db_pool = cache_db_pool
        self.main_db_pool = main_db_pool
