class DataVendorUnavailable(Exception):
    """Raised when a vendor cannot serve a request and fallback should be attempted."""


class MissingEtfHoldings(DataVendorUnavailable):
    """Raised when an ETF has no disclosed equity holdings for the requested date."""
