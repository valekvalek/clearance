import os
import sys
from yandex_ai_studio_sdk import AIStudio
from yandex_ai_studio_sdk.auth import YandexCloudCLIAuth

folder_id = os.environ["YC_FOLDER_ID"]
dataset_path = sys.argv[1] if len(sys.argv) > 1 else "training/finetune_tnved.jsonl"

sdk = AIStudio(folder_id=folder_id, auth=YandexCloudCLIAuth(yc_profile="default"))

dataset_draft = sdk.datasets.draft_from_path(
    task_type="TextToTextGeneration",
    path=dataset_path,
    upload_format="jsonlines",
    name="tnved-classifier-dataset",
)
operation = dataset_draft.upload_deferred()
dataset = operation.wait()

print("DATASET_ID:", dataset.id)
print("STATUS:", dataset.status)
