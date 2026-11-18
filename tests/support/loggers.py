import logging


def all_loggers() -> list[logging.Logger]:
    """The root logger and every logger the process has created so far."""
    created = [
        logger
        for logger in logging.Logger.manager.loggerDict.values()
        if isinstance(logger, logging.Logger)
    ]
    return [logging.getLogger(), *created]


def logger_settings() -> dict[str, tuple[int, bool, tuple[object, ...], tuple[object, ...]]]:
    """Level, propagation, handlers and filters of every logger that differs from a new one.

    A logger at a new logger's settings is left out, so one that exists only because something
    asked for it by name equals one that was never created.
    """
    settings = {
        logger.name: (logger.level, logger.propagate, tuple(logger.handlers), tuple(logger.filters))
        for logger in all_loggers()
    }
    new_logger = (logging.NOTSET, True, (), ())
    return {name: setting for name, setting in settings.items() if setting != new_logger}
