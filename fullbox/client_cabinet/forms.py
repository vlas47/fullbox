import re

from django import forms
from django.core.exceptions import ValidationError
from django.core.validators import URLValidator

from sku.models import Agency


def _normalize_text(value: str | None) -> str | None:
    if value is None:
        return None
    text = re.sub(r"\s+", " ", str(value)).strip()
    return text or None


def _format_phone(digits: str) -> str:
    if not digits:
        return ""
    main = digits[1:]
    chunks = [
        main[:3],
        main[3:6],
        main[6:8],
        main[8:10],
    ]
    if len(chunks[0]) < 3:
        return f"+7 ({chunks[0]}"
    formatted = f"+7 ({chunks[0]})"
    if chunks[1]:
        formatted += f" {chunks[1]}"
    if chunks[2]:
        formatted += f"-{chunks[2]}"
    if chunks[3]:
        formatted += f"-{chunks[3]}"
    return formatted


class AgencyForm(forms.ModelForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        required_fields = ("inn", "phone", "fio_agn")
        for name in required_fields:
            field = self.fields.get(name)
            if field:
                field.required = True
                field.widget.attrs["required"] = "required"
        phone_field = self.fields.get("phone")
        if phone_field:
            phone_field.widget.attrs.setdefault("inputmode", "tel")
            phone_field.widget.attrs.setdefault("placeholder", "+7 (___) ___-__-__")

    def clean_agn_name(self):
        value = _normalize_text(self.cleaned_data.get("agn_name"))
        if value and len(value) < 2:
            raise forms.ValidationError("Название организации слишком короткое.")
        return value

    def clean_pref(self):
        return _normalize_text(self.cleaned_data.get("pref"))

    def clean_inn(self):
        inn_raw = _normalize_text(self.cleaned_data.get("inn")) or ""
        if not inn_raw:
            raise forms.ValidationError("ИНН обязателен.")
        inn = re.sub(r"\D", "", inn_raw)
        if not inn.isdigit():
            raise forms.ValidationError("ИНН должен содержать только цифры.")
        if inn and len(inn) not in (10, 12):
            raise forms.ValidationError("ИНН должен быть 10 или 12 цифр.")
        if inn:
            qs = Agency.objects.filter(inn=inn)
            if self.instance.pk:
                qs = qs.exclude(pk=self.instance.pk)
            if qs.exists():
                raise forms.ValidationError("Такой ИНН уже есть в системе.")
        return inn

    def clean_kpp(self):
        value = _normalize_text(self.cleaned_data.get("kpp"))
        if not value:
            return None
        digits = re.sub(r"\D", "", value)
        if not digits.isdigit() or len(digits) != 9:
            raise forms.ValidationError("КПП должен содержать 9 цифр.")
        return digits

    def clean_ogrn(self):
        value = _normalize_text(self.cleaned_data.get("ogrn"))
        if not value:
            return None
        digits = re.sub(r"\D", "", value)
        if not digits.isdigit() or len(digits) not in (13, 15):
            raise forms.ValidationError("ОГРН должен содержать 13 или 15 цифр.")
        return digits

    def clean_phone(self):
        value = _normalize_text(self.cleaned_data.get("phone")) or ""
        if not value:
            raise forms.ValidationError("Телефон обязателен.")
        digits = re.sub(r"\D", "", value)
        if len(digits) == 10:
            digits = "7" + digits
        elif len(digits) == 11 and digits.startswith("8"):
            digits = "7" + digits[1:]
        if len(digits) != 11 or not digits.startswith("7"):
            raise forms.ValidationError("Телефон должен начинаться с +7 и содержать 11 цифр.")
        return _format_phone(digits)

    def clean_email(self):
        return _normalize_text(self.cleaned_data.get("email"))

    def clean_adres(self):
        return _normalize_text(self.cleaned_data.get("adres"))

    def clean_fakt_adres(self):
        return _normalize_text(self.cleaned_data.get("fakt_adres"))

    def clean_fio_agn(self):
        value = _normalize_text(self.cleaned_data.get("fio_agn")) or ""
        if not value:
            raise forms.ValidationError("ФИО контактного обязательно.")
        if re.search(r"\d", value):
            raise forms.ValidationError("ФИО не должно содержать цифры.")
        if len(value) < 3:
            raise forms.ValidationError("ФИО слишком короткое.")
        return value

    def clean_contract_numb(self):
        return _normalize_text(self.cleaned_data.get("contract_numb"))

    def clean_contract_link(self):
        value = _normalize_text(self.cleaned_data.get("contract_link"))
        if not value:
            return None
        validator = URLValidator()
        try:
            validator(value)
        except ValidationError as exc:
            raise forms.ValidationError("Ссылка на договор должна быть корректным URL.") from exc
        return value

    def clean(self):
        cleaned = super().clean()
        cleaned["agn_name"] = _normalize_text(cleaned.get("agn_name"))
        cleaned["pref"] = _normalize_text(cleaned.get("pref"))
        cleaned["adres"] = _normalize_text(cleaned.get("adres"))
        cleaned["fakt_adres"] = _normalize_text(cleaned.get("fakt_adres"))
        cleaned["contract_numb"] = _normalize_text(cleaned.get("contract_numb"))
        cleaned["contract_link"] = _normalize_text(cleaned.get("contract_link"))
        cleaned["email"] = _normalize_text(cleaned.get("email"))
        return cleaned

    class Meta:
        model = Agency
        fields = [
            "agn_name",
            "pref",
            "inn",
            "kpp",
            "ogrn",
            "phone",
            "email",
            "adres",
            "fakt_adres",
            "fio_agn",
            "sign_oferta",
            "use_nds",
            "contract_numb",
            "contract_link",
            "archived",
        ]
        widgets = {
            "adres": forms.Textarea(attrs={"rows": 2}),
            "fakt_adres": forms.Textarea(attrs={"rows": 2}),
        }
