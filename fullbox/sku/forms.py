from django import forms

from .models import SKU


class SKUForm(forms.ModelForm):
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
