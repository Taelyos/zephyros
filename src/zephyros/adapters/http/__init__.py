"""HTTP adapters: the three listeners of the hub (agent endpoint, main-agent endpoint,
operator channel). They authenticate from the token and the TCP peer address, call the hub, and
translate the answer. They read no forwarded header and keep no session."""
