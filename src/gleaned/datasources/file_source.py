import pandas as pd
from .base import DataSource

class FileSource(DataSource):
    def __init__(self, file_path: str, file_type: str):
        """
        Data source for loading data from a file.
        :param file_path: Path to the file.
        :param file_type: Type of the file (csv, parquet, json).
        """
        super().__init__(refresh_interval=0)  # Static file sources have no refresh interval
        self.file_path = file_path
        self.file_type = file_type.lower()

    def collect_data(self) -> pd.DataFrame:
        """Load data from the specified file."""
        try:
            print(f"Loading data from {self.file_type.upper()} file: {self.file_path}")
            if self.file_type == "csv":
                data = pd.read_csv(self.file_path)
            elif self.file_type == "parquet":
                data = pd.read_parquet(self.file_path)
            elif self.file_type == "json":
                data = pd.read_json(self.file_path)
            else:
                raise ValueError(f"Unsupported file type: {self.file_type}")

            print(f"Loaded {len(data)} rows from {self.file_path}")
            return data
        except Exception as e:
            print(f"Error while loading {self.file_type.upper()} file: {e}")
            return pd.DataFrame()

    def metadata(self) -> dict:
        """Return metadata about the file data source."""
        return {
            "source": f"{self.file_type.upper()} File: {self.file_path}",
            "type": "static",
            "file_path": self.file_path,
            "file_type": self.file_type,
        }
