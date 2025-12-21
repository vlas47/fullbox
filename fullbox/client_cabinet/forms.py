from django import forms

from sku.models import Agency


class AgencyForm(forms.ModelForm):
    def clean_inn(self):
        inn = (self.cleaned_data.get("inn") or "").strip()
        if inn and not inn.isdigit():
            raise forms.ValidationError("ИНН должен содержать только цифры.")
        if inn and len(inn) not in (10, 12):
            raise forms.ValidationError("ИНН должен быть 10 или 12 цифр.")
        if inn:
            qs = Agency.objects.filter(inn=inn)
            if self.instance.pk:
                qs = qs.exclude(pk=self.instance.pk)
            if qs.exists():
                raise forms.ValidationError("Такой ИНН уже есть в системе.")
        return inn or None

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
