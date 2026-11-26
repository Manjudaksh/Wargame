from django.core.management.base import BaseCommand, CommandError

from vectordata.pipeline import process


class Command(BaseCommand):
    help = 'Process one queued vector dataset (started automatically after an upload).'

    def add_arguments(self, parser):
        parser.add_argument('dataset_id', type=int)

    def handle(self, *args, dataset_id, **options):
        if not process(dataset_id):
            raise CommandError(f'dataset {dataset_id} was not processed (not queued, or failed: see the Admin page)')
        self.stdout.write(f'dataset {dataset_id} processed')
