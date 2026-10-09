from django.core.management.base import BaseCommand, CommandError

from archive.backup import create_backup_snapshot
from archive.models import BackupSnapshot


class Command(BaseCommand):
    help = "Create and verify a dated Dropbox backup (no restore or retention)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--trigger",
            choices=BackupSnapshot.Trigger.values,
            default=BackupSnapshot.Trigger.SCHEDULED,
        )

    def handle(self, *args, **options):
        result = create_backup_snapshot(trigger=options["trigger"])
        if not result.success:
            raise CommandError(result.message)
        self.stdout.write(self.style.SUCCESS(result.message))
