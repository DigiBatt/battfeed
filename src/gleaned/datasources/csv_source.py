import pandas as pd
from .base import DataSource

class CSVSource(DataSource):
    def __init__(self, file_path: str):
        """
        Data source for loading data from a CSV file.
        :param file_path: Path to the CSV file.
        """
        super().__init__(refresh_interval=0)  # CSV is static, so no refresh interval
        self.file_path = file_path

    def collect_data(self) -> pd.DataFrame:
        """Load data from the CSV file."""
        try:
            print(f"Loading data from CSV file: {self.file_path}")
            data = pd.read_csv(self.file_path)
            print(f"Loaded {len(data)} rows from {self.file_path}")
            return data
        except Exception as e:
            print(f"Error while loading CSV file: {e}")
            return pd.DataFrame()

    def metadata(self) -> dict:
        """Return metadata about the CSV data source."""
        return {
            "source": f"CSV File: {self.file_path}",
            "type": "static",
            "file_path": self.file_path,
        }
