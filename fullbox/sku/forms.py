from django import forms

from .models import SKU


class SKUForm(forms.ModelForm):
    def clean(self):
        cleaned = super().clean()
        sku_code = (cleaned.get("sku_code") or "").strip()
        agency = cleaned.get("agency")
        code = (cleaned.get("code") or "").strip()

        if not sku_code:
            self.add_error("sku_code", "Артикул обязателен.")

        if not code:
            self.add_error("code", "Штрихкод обязателен.")

        if sku_code:
            qs = SKU.objects.filter(deleted=False, sku_code=sku_code)
            if agency is None:
                qs = qs.filter(agency__isnull=True)
            else:
                qs = qs.filter(agency=agency)
            if self.instance.pk:
                qs = qs.exclude(pk=self.instance.pk)
            if qs.exists():
                self.add_error("sku_code", "Такой артикул уже есть у этого клиента.")

        numeric_fields = {
            "weight_kg": "Вес",
            "volume": "Объем",
            "length_mm": "Длина",
            "width_mm": "Ширина",
            "height_mm": "Высота",
        }
        for field, label in numeric_fields.items():
            value = cleaned.get(field)
            if value is not None and value < 0:
                self.add_error(field, f"{label} не может быть отрицательным.")

        return cleaned
    class Meta:
        model = SKU
        fields = [
            "sku_code",
            "name",
            "brand",
            "agency",
            "market",
            "color",
            "color_ref",
            "size",
            "name_print",
            "code",
            "img",
            "img_comment",
            "gender",
            "season",
            "additional_name",
            "composition",
            "made_in",
            "cr_product_date",
            "end_product_date",
            "sign_akciz",
            "tovar_category",
            "use_nds",
            "vid_tovar",
            "type_tovar",
            "stor_unit",
            "weight_kg",
            "volume",
            "length_mm",
            "width_mm",
            "height_mm",
            "honest_sign",
            "description",
            "source",
            "source_reference",
        ]
        widgets = {
            "cr_product_date": forms.DateInput(attrs={"type": "date"}),
            "end_product_date": forms.DateInput(attrs={"type": "date"}),
            "description": forms.Textarea(attrs={"rows": 3}),
            "img_comment": forms.Textarea(attrs={"rows": 2}),
        }
