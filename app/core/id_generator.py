from snowflake import SnowflakeGenerator

# In actual production, worker_id can be obtained from environment variables or the container's hostname hash.
# Here, it's set to 1 by default.
generator = SnowflakeGenerator(instance=1)


def next_id() -> int:
    """Generate the next snowflake ID"""
    return next(generator)
