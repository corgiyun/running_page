"""Download activities from Garmin Connect.

The project historically used ``garth`` directly. Garmin changed its
consumer authentication flow and Garth is no longer maintained, so this file
keeps the old async-facing interface while delegating authentication and API
calls to the maintained ``garminconnect`` client.
"""

import argparse
import asyncio
import os
import sys
import time
import traceback
import zipfile
from io import BytesIO

import aiofiles
from garminconnect import (
    Garmin as GarminConnectClient,
    GarminConnectAuthenticationError,
)

from config import FOLDER_DICT, JSON_FILE, SQL_FILE, config
from utils import make_activities_file


class Garmin:
    """Async compatibility adapter around python-garminconnect."""

    def __init__(self, secret_string, auth_domain, is_only_running=False):
        if not secret_string:
            raise GarminConnectAuthenticationError("Missing Garmin token")

        self.is_cn = bool(auth_domain and str(auth_domain).upper() == "CN")
        self.is_only_running = is_only_running
        self.client = GarminConnectClient(is_cn=self.is_cn)
        self.client.login(tokenstore=str(secret_string).strip())

    async def get_activities(self, start, limit):
        activity_type = "running" if self.is_only_running else None
        return await asyncio.to_thread(
            self.client.get_activities,
            start=start,
            limit=limit,
            activitytype=activity_type,
        )

    async def get_activity_summary(self, activity_id):
        return await asyncio.to_thread(self.client.get_activity, str(activity_id))

    async def download_activity(self, activity_id, file_type="gpx"):
        formats = {
            "gpx": self.client.ActivityDownloadFormat.GPX,
            "tcx": self.client.ActivityDownloadFormat.TCX,
            "fit": self.client.ActivityDownloadFormat.ORIGINAL,
        }
        if file_type not in formats:
            raise ValueError(f"Unsupported Garmin download format: {file_type}")
        return await asyncio.to_thread(
            self.client.download_activity,
            str(activity_id),
            formats[file_type],
        )

    async def upload_activity_from_file(self, file):
        print("Uploading " + str(file))
        return await asyncio.to_thread(self.client.upload_activity, str(file))

    async def upload_activities_files(self, files):
        print("start upload activities to garmin!")
        for file in files:
            try:
                result = await self.upload_activity_from_file(file)
                print("garmin upload success: ", result)
            except Exception as error:
                print(f"garmin upload failed for {file}: {error}")

    async def upload_activities_original_from_strava(
        self, datas, use_fake_garmin_device=False
    ):
        """Preserve the legacy upload interface used by Strava-to-Garmin."""
        if use_fake_garmin_device:
            raise ValueError(
                "use_fake_garmin_device is not supported by the new Garmin client"
            )

        temporary_files = []
        try:
            for data in datas:
                print(data.filename)
                with open(data.filename, "wb") as output:
                    for chunk in data.content:
                        output.write(chunk)
                temporary_files.append(data.filename)
                try:
                    result = await self.upload_activity_from_file(data.filename)
                    print("garmin upload success: ", result)
                except Exception as error:
                    print(f"garmin upload failed for {data.filename}: {error}")
        finally:
            for filename in temporary_files:
                try:
                    os.remove(filename)
                except FileNotFoundError:
                    pass

    async def aclose(self):
        """Close the underlying requests sessions when available."""
        for session_name in ("cs", "_api_session"):
            session = getattr(self.client.client, session_name, None)
            if session is not None:
                await asyncio.to_thread(session.close)


async def download_garmin_data(client, activity_id, file_type="gpx"):
    folder = FOLDER_DICT.get(file_type, FOLDER_DICT["gpx"])
    try:
        file_data = await client.download_activity(activity_id, file_type=file_type)

        if file_type != "fit":
            file_path = os.path.join(folder, f"{activity_id}.{file_type}")
            async with aiofiles.open(file_path, "wb") as file_handle:
                await file_handle.write(file_data)
            return True

        # The ORIGINAL endpoint returns a ZIP. Write only known activity files
        # to the project folders instead of extracting arbitrary ZIP paths.
        with zipfile.ZipFile(BytesIO(file_data), "r") as zip_file:
            extracted = False
            for member in zip_file.infolist():
                if member.is_dir():
                    continue
                extension = os.path.splitext(member.filename)[1].lower()
                if extension == ".fit":
                    target_folder = FOLDER_DICT["fit"]
                    target_path = os.path.join(target_folder, f"{activity_id}.fit")
                elif extension == ".gpx":
                    target_folder = FOLDER_DICT["gpx"]
                    target_path = os.path.join(target_folder, f"{activity_id}.gpx")
                else:
                    continue
                async with aiofiles.open(target_path, "wb") as file_handle:
                    await file_handle.write(zip_file.read(member))
                extracted = True

        if not extracted:
            raise ValueError("Garmin original download contained no FIT or GPX file")
        return True
    except Exception as error:
        print(f"Failed to download activity {activity_id}: {error}")
        traceback.print_exc()
        return False


async def get_activity_id_list(client, start=0):
    activity_ids = []
    page_size = 100
    while True:
        activities = await client.get_activities(start, page_size)
        if isinstance(activities, dict):
            activities = activities.get("activityList", [])
        activities = activities or []
        if not activities:
            break

        activity_ids.extend(
            str(activity.get("activityId"))
            for activity in activities
            if activity.get("activityId") is not None
        )
        print("Syncing Activity IDs")
        start += page_size
    return activity_ids


async def gather_with_concurrency(n, tasks):
    semaphore = asyncio.Semaphore(n)

    async def sem_task(task):
        async with semaphore:
            return await task

    return await asyncio.gather(*(sem_task(task) for task in tasks))


def get_downloaded_ids(folder):
    if not os.path.isdir(folder):
        return []
    return [i.split(".")[0] for i in os.listdir(folder) if not i.startswith(".")]


async def download_new_activities(
    secret_string, auth_domain, downloaded_ids, is_only_running, folder, file_type
):
    client = Garmin(secret_string, auth_domain, is_only_running)
    try:
        activity_ids = await get_activity_id_list(client)
        to_download = sorted(
            set(activity_ids) - set(downloaded_ids),
            key=lambda activity_id: int(activity_id),
        )
        print(f"{len(to_download)} new activities to be downloaded")

        id_to_title = {}
        for activity_id in to_download:
            try:
                summary = await client.get_activity_summary(activity_id)
                id_to_title[activity_id] = summary.get("activityName", "")
            except Exception as error:
                print(f"Failed to get activity summary {activity_id}: {error}")

        start_time = time.time()
        results = await gather_with_concurrency(
            5,
            [
                download_garmin_data(client, activity_id, file_type=file_type)
                for activity_id in to_download
            ],
        )
        failed = sum(result is False for result in results)
        print(f"Download finished. Elapsed {time.time() - start_time:.1f} seconds")
        if failed:
            print(f"Warning: {failed} activity download(s) failed and will retry later")
        return to_download, id_to_title
    finally:
        await client.aclose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "secret_string", nargs="?", help="JSON token from get_garmin_secret.py"
    )
    parser.add_argument(
        "--is-cn", dest="is_cn", action="store_true", help="use Garmin China"
    )
    parser.add_argument(
        "--only-run", dest="only_run", action="store_true", help="only running"
    )
    parser.add_argument(
        "--tcx",
        dest="download_file_type",
        action="store_const",
        const="tcx",
        default="gpx",
        help="download TCX instead of GPX",
    )
    parser.add_argument(
        "--fit",
        dest="download_file_type",
        action="store_const",
        const="fit",
        default="gpx",
        help="download original FIT instead of GPX",
    )
    options = parser.parse_args()
    if options.secret_string is None:
        print("Missing Garmin token JSON")
        sys.exit(1)

    auth_domain = (
        "CN" if options.is_cn else config("sync", "garmin", "authentication_domain")
    )
    file_type = options.download_file_type
    folder = FOLDER_DICT.get(file_type, FOLDER_DICT["gpx"])
    os.makedirs(folder, exist_ok=True)
    downloaded_ids = get_downloaded_ids(folder)

    if file_type == "fit":
        os.makedirs(FOLDER_DICT["gpx"], exist_ok=True)
        downloaded_ids = list(
            set(downloaded_ids + get_downloaded_ids(FOLDER_DICT["gpx"]))
        )

    _, id_to_title = asyncio.run(
        download_new_activities(
            options.secret_string,
            auth_domain,
            downloaded_ids,
            options.only_run,
            folder,
            file_type,
        )
    )

    if file_type == "fit":
        make_activities_file(
            SQL_FILE,
            FOLDER_DICT["gpx"],
            JSON_FILE,
            file_suffix="gpx",
            activity_title_dict=id_to_title,
        )
    make_activities_file(
        SQL_FILE,
        folder,
        JSON_FILE,
        file_suffix=file_type,
        activity_title_dict=id_to_title,
    )
