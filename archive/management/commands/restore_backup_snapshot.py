from django.core.management.base import BaseCommand, CommandError

from archive.restore import restore_backup


class Command(BaseCommand):
    help = (
        "Verify a backup and publish a new recovery directory; never replace live data."
    )
    requires_system_checks = ()

    def add_arguments(self, parser):
        parser.add_argument("source")
        parser.add_argument("target")
        parser.add_argument("--local-root", required=True)
        parser.add_argument("--icloud-root", required=True)
        parser.add_argument("--dropbox-root", required=True)

    def handle(self, *args, **options):
        result = restore_backup(
            options["source"],
            options["target"],
            local_root=options["local_root"],
            icloud_root=options["icloud_root"],
            dropbox_root=options["dropbox_root"],
        )
        if not result.success:
            raise CommandError(result.message)
        self.stdout.write(
            self.style.SUCCESS(f"{result.message} {result.target_directory}")
        )
