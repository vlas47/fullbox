import re

from django import forms

from .models import Carrier, OwnCompany


def _normalize_text(value):
    return re.sub(r"\s+", " ", str(value or "")).strip()


class _ReferenceBaseForm(forms.ModelForm):
    textareas = ("address", "postal_address", "bank_address", "comment")

    def clean(self):
        cleaned = super().clean()
        for key, value in list(cleaned.items()):
            if isinstance(value, str):
                cleaned[key] = _normalize_text(value)
        return cleaned

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field_name in self.textareas:
            field = self.fields.get(field_name)
            if field:
                field.widget = forms.Textarea(attrs={"rows": 3})


class OwnCompanyForm(_ReferenceBaseForm):
    class Meta:
        model = OwnCompany
        fields = [
            "name",
            "inn",
            "kpp",
            "ogrn",
            "address",
            "postal_address",
            "phone",
            "email",
            "director_name",
            "director_basis",
            "bank_name",
            "bank_bik",
            "settlement_account",
            "correspondent_account",
            "bank_address",
            "edo_operator",
            "edo_id",
            "is_default",
            "is_active",
            "comment",
        ]


class CarrierForm(_ReferenceBaseForm):
    class Meta:
        model = Carrier
        fields = [
            "name",
            "inn",
            "kpp",
            "ogrn",
            "address",
            "postal_address",
            "phone",
            "email",
            "contact_person",
            "bank_name",
            "bank_bik",
            "settlement_account",
            "correspondent_account",
            "bank_address",
            "is_active",
            "comment",
        ]
