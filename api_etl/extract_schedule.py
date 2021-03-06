"""
Module used to download from SNCF website trains schedules and save it in the right format
in different databases (Dynamo or relational database)
"""

from os import path, makedirs
import zipfile
from urllib.request import urlretrieve
import logging

import pandas as pd

from api_etl.settings import __GTFS_FOLDER_PATH__, __GTFS_CSV_URL__, __DATA_PATH__
from api_etl.utils_rdb import RdbProvider
from api_etl.data_models import (
    Agency,
    Route,
    Trip,
    StopTime,
    Stop,
    Calendar,
    CalendarDate,
)
from api_etl.utils_misc import get_paris_local_datetime_now, S3Bucket
from api_etl.settings import __S3_BUCKETS__

logger = logging.getLogger(__name__)
pd.options.mode.chained_assignment = None


class ScheduleExtractor:
    """ Common class for schedule extractors
    """

    def __init__(self):

        self.gtfs_folder = __GTFS_FOLDER_PATH__
        self.schedule_url = __GTFS_CSV_URL__

        self.files_present = None
        self._check_files()

    def _check_files(self):

        files_to_check = [
            "gtfs-lines-last/calendar.txt",
            "gtfs-lines-last/trips.txt",
            "gtfs-lines-last/stop_times.txt",
            "gtfs-lines-last/stops.txt",
            "gtfs-lines-last/calendar_dates.txt",
        ]
        # Default: True, and if one missing -> False
        self.files_present = True
        for file_check in files_to_check:
            try:
                pd.read_csv(
                    path.join(self.gtfs_folder, file_check)
                )

            except FileNotFoundError:
                logger.warning("File %s not found in data folder %s" %
                                (file_check, self.gtfs_folder))
                self.files_present = False
        return self.files_present

    def download_gtfs_files(self):
        """
        Download gtfs files from SNCF website (based on URL defined in settings module) and saves it in data folder
        (defined as well in settings module). There is no paramater to pass.

        Process is in two steps:
        - first: download csv file containing links to zip files
        - second: download files based on urls found in csv from first step

        Folder names in which files are unzip are based on the headers of the zip files.

        Function returns True if 'gtfs-lines-last' folder has been found (this is the usual folder we use then to find
        schedules). Return False otherwise.

        :rtype: boolean
        """
        logger.info(
            "Download of csv containing links of zip files, at url %s", self.schedule_url)
        gtfs_links = pd.read_csv(self.schedule_url)

        # Create data folder if necessary
        if not path.exists(self.gtfs_folder):
            makedirs(self.gtfs_folder)

        # Download and unzip all files
        # Check if one is "gtfs-lines-last"
        gtfs_lines_last_present = False

        for link in gtfs_links["file"].values:
            logger.info("Download of %s", link)

            local_filename, headers = urlretrieve(link)
            logger.info("File name is %s", headers.get_filename())

            # Get name in header and remove the ".zip"
            extracted_data_folder_name = headers.get_filename().split(".")[0]
            if extracted_data_folder_name == "gtfs-lines-last":
                gtfs_lines_last_present = True

            with zipfile.ZipFile(local_filename, "r") as zip_ref:
                full_path = path.join(
                    self.gtfs_folder, extracted_data_folder_name)
                zip_ref.extractall(path=full_path)

            if gtfs_lines_last_present:
                logger.info("The 'gtfs-lines-last' folder has been found.")
            else:
                logger.error(
                    "The 'gtfs-lines-last' folder has not been found! Schedules will not be updated.")

        return gtfs_lines_last_present

    def save_gtfs_in_s3(self):
        day = get_paris_local_datetime_now().strftime("%Y%m%d")
        sb = S3Bucket(__S3_BUCKETS__["gtfs-files"], create_if_absent=True)

        sb.send_folder(
            folder_local_path=self.gtfs_folder,
            folder_remote_path=day
        )


class ScheduleExtractorRDB(ScheduleExtractor):
    """ For relational database
    """

    def __init__(self, dsn=None):
        ScheduleExtractor.__init__(self)

        self.dsn = dsn
        self.rdb_provider = RdbProvider(self.dsn)

    def save_in_rdb(self, tables=None):
        assert self.files_present

        to_save = [
            ("agency.txt", Agency),
            ("routes.txt", Route),
            ("trips.txt", Trip),
            ("stops.txt", Stop),
            ("stop_times.txt", StopTime),
            ("calendar.txt", Calendar),
            ("calendar_dates.txt", CalendarDate)
        ]
        if tables:
            assert isinstance(tables, list)
            to_save = [to_save[i] for i in tables]

        for name, model in to_save:
            df = pd.read_csv(path.join(self.gtfs_folder, name))
            df = df.applymap(str)
            dicts = df.to_dict(orient="records")
            objects = list(map(lambda x: model(**x), dicts))
            logger.info("Saving %s file in database, containing %s objects." % (
                name, len(objects)))
            session = self.rdb_provider.get_session()
            try:
                # Try to save bulks (initial load)
                chunks = [objects[i:i + 100]
                          for i in range(0, len(objects), 100)]
                for chunk in chunks:
                    logger.debug("Bulk of 100 items saved.")
                    session.bulk_save_objects(chunk)
                    session.commit()
            except Exception:
                # Or save items one after the other
                session.rollback()
                for obj in objects:
                    session.merge(obj)
                    session.commit()
            session.close()
