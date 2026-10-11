"""便捷启动入口（等价于 ``python claude_api_server.py``）。"""


def main() -> None:
    import uvicorn

    from claude_web import config
    from claude_web.logging_setup import configure_logging
    from claude_web.server import app

    configure_logging()
    uvicorn.run(app, host=config.HOST, port=config.PORT)


if __name__ == "__main__":
    main()
