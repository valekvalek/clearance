import os
import sys
from yandex_ai_studio_sdk import AIStudio
from yandex_ai_studio_sdk.auth import YandexCloudCLIAuth

folder_id = os.environ["YC_FOLDER_ID"]
dataset_id = sys.argv[1]

sdk = AIStudio(folder_id=folder_id, auth=YandexCloudCLIAuth(yc_profile="default"))
dataset = sdk.datasets.get(dataset_id)
print("ID:", dataset.id)
print("NAME:", dataset.name)
print("STATUS:", dataset.status)
