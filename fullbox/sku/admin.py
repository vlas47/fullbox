from django.contrib import admin

from .models import (
    Agency,
    Color,
    Market,
    MarketCredential,
    MarketplaceBinding,
    SKU,
    SKUBarcode,
    SKUPhoto,
    Store,
)


class SKUBarcodeInline(admin.TabularInline):
    model = SKUBarcode
    extra = 1


class SKUPhotoInline(admin.TabularInline):
    model = SKUPhoto
    extra = 1


class MarketplaceBindingInline(admin.TabularInline):
    model = MarketplaceBinding
    extra = 0


@admin.register(SKU)
class SKUAdmin(admin.ModelAdmin):
    list_display = (
        "sku_code",
        "name",
        "brand",
        "agency",
        "market",
        "color_ref",
        "size",
        "honest_sign",
        "use_nds",
        "updated_at",
    )
    search_fields = ("sku_code", "name", "barcodes__value", "brand")
    list_filter = ("source", "honest_sign", "use_nds", "market", "color_ref")
    inlines = [SKUBarcodeInline, SKUPhotoInline, MarketplaceBindingInline]
    ordering = ("sku_code",)


@admin.register(SKUBarcode)
class SKUBarcodeAdmin(admin.ModelAdmin):
    list_display = ('value', 'sku', 'is_primary')
    list_filter = ('is_primary',)
    search_fields = ('value', 'sku__sku_code', 'sku__name')


@admin.register(SKUPhoto)
class SKUPhotoAdmin(admin.ModelAdmin):
    list_display = ('sku', 'url', 'sort_order')
    ordering = ('sort_order',)


@admin.register(MarketplaceBinding)
class MarketplaceBindingAdmin(admin.ModelAdmin):
    list_display = ("sku", "marketplace", "external_id", "sync_mode", "last_synced_at")
    list_filter = ("marketplace", "sync_mode")
    search_fields = ("external_id", "sku__sku_code", "sku__name")


@admin.register(Market)
class MarketAdmin(admin.ModelAdmin):
    list_display = ("id", "name")
    ordering = ("id",)


@admin.register(Color)
class ColorAdmin(admin.ModelAdmin):
    list_display = ("id", "name")
    ordering = ("id",)


@admin.register(Agency)
class AgencyAdmin(admin.ModelAdmin):
    list_display = ("id", "agn_name", "inn", "email", "phone", "use_nds")
    search_fields = ("agn_name", "inn", "pref", "email")
    list_filter = ("use_nds",)


@admin.register(Store)
class StoreAdmin(admin.ModelAdmin):
    list_display = ("id", "stor_name", "agency")
    search_fields = ("stor_name", "agn_name")


@admin.register(MarketCredential)
class MarketCredentialAdmin(admin.ModelAdmin):
    list_display = ("id", "agency", "market")
    search_fields = ("agency__agn_name",)
