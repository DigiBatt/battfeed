from abc import ABC, abstractmethod
import pandas as pd

# Abstract Base Class for Data Sources
class DataSource(ABC):
    def __init__(self, refresh_interval: int = None):
        """
        Initialize a data source.
        :param refresh_interval: Interval (in seconds) for refreshing live data. None for static sources.
        """
        self.refresh_interval = refresh_interval

    @abstractmethod
    def collect_data(self) -> pd.DataFrame:
        """Collect data from the source and return as a DataFrame."""
        pass

    @abstractmethod
    def metadata(self) -> dict:
        """Return metadata about the data source."""
        pass