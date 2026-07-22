from django.apps import apps
from django.core.management.base import BaseCommand, CommandError

from accounts.models import ArriveHandoffToken, Customer, User
from banking.models import BankAccount, BankConnection, BankTransaction
from loans.models import Loan


class Command(BaseCommand):
    help = "Fully purge Arrive test customer(s) by email (and optional zum user id)."

    def add_arguments(self, parser):
        parser.add_argument("--email", action="append", required=True)
        parser.add_argument("--zum-user-id", default="")
        parser.add_argument("--confirm", required=True, help='Must be "DELETE"')

    def handle(self, *args, **options):
        if options["confirm"] != "DELETE":
            raise CommandError('Pass --confirm DELETE to proceed.')

        emails = [e.strip().lower() for e in options["email"] if e and e.strip()]
        zum = (options["zum_user_id"] or "").strip()

        targets = Customer.objects.none()
        for email in emails:
            targets = targets | Customer.objects.filter(email__iexact=email)
        if zum:
            targets = targets | Customer.objects.filter(arrive_zum_user_id=zum)
        targets = targets.distinct()

        self.stdout.write(f"TARGETS {targets.count()}")
        deleted_ids = []

        for customer in list(targets):
            portal = customer.portal_user
            email = customer.email
            self.stdout.write(
                "DELETING_CUSTOMER "
                f"{customer.id} {email} {customer.arrive_application_id} "
                f"loans={list(customer.loans.values_list('id', flat=True))} "
                f"conns={list(customer.bank_connections.values_list('id', flat=True))}"
            )
            deleted_ids.append(str(customer.id))
            customer.delete()
            if portal is not None:
                self.stdout.write(f"DELETING_PORTAL {portal.id} {portal.email}")
                portal.delete()
            for user in User.objects.filter(email__iexact=email):
                self.stdout.write(f"DELETING_USER {user.id} {user.email}")
                user.delete()

        for email in emails:
            for user in User.objects.filter(email__iexact=email):
                self.stdout.write(f"SWEEP_USER {user.id} {user.email}")
                user.delete()

        leftovers = []
        for model in apps.get_models():
            field_names = {f.name for f in model._meta.fields}
            if "customer" not in field_names:
                continue
            label = f"{model._meta.app_label}.{model.__name__}"
            for cid in deleted_ids:
                try:
                    count = model.objects.filter(customer_id=cid).count()
                    if count:
                        leftovers.append(f"{label}:{cid}:{count}")
                except Exception as exc:
                    leftovers.append(f"{label}:{cid}:ERROR:{exc}")

        for email in emails:
            self.stdout.write(
                f"VERIFY_CUSTOMER_EMAIL {email}={Customer.objects.filter(email__iexact=email).count()}"
            )
            self.stdout.write(
                f"VERIFY_USER_EMAIL {email}={User.objects.filter(email__iexact=email).count()}"
            )
        if zum:
            self.stdout.write(
                f"VERIFY_ZUM {zum}={Customer.objects.filter(arrive_zum_user_id=zum).count()}"
            )
        for cid in deleted_ids:
            self.stdout.write(
                f"VERIFY_ID {cid} "
                f"customer={Customer.objects.filter(id=cid).exists()} "
                f"handoff={ArriveHandoffToken.objects.filter(customer_id=cid).count()} "
                f"loans={Loan.objects.filter(customer_id=cid).count()} "
                f"conns={BankConnection.objects.filter(customer_id=cid).count()} "
                f"accounts={BankAccount.objects.filter(customer_id=cid).count()} "
                f"tx={BankTransaction.objects.filter(customer_id=cid).count()}"
            )

        self.stdout.write(f"LEFTOVERS {leftovers}")
        self.stdout.write(self.style.SUCCESS("DONE"))
