from django.conf import settings
from django.core.management import BaseCommand, call_command


class Command(BaseCommand):
    help = "Run Django migrations for every configured tenant database."

    def add_arguments(self, parser):
        parser.add_argument(
            "--skip-default",
            action="store_true",
            help="Migrate only non-default tenant database aliases.",
        )

    def handle(self, *args, **options):
        aliases = list(settings.TENANT_DATABASE_ALIASES)
        if options["skip_default"]:
            aliases = [alias for alias in aliases if alias != "default"]

        for alias in aliases:
            self.stdout.write(self.style.MIGRATE_HEADING(f"Migrating database '{alias}'"))
            call_command("migrate", database=alias, interactive=False, verbosity=options["verbosity"])
