from alert_channels.millerbot import MillerBotChannel

_CHANNEL_TYPES = {
    "millerbot": MillerBotChannel,
}


def channelFactory(logger, cfg):
    """Instantiate an alert channel from its configuration object."""
    channel_type = cfg.type
    cls = _CHANNEL_TYPES.get(channel_type)
    if cls is None:
        raise ValueError(f"Unknown alert channel type: '{channel_type}'")
    return cls(logger, cfg)
