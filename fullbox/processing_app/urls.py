from django.urls import path

from .views import (
    ProcessingDetailView,
    ProcessingHomeView,
    ProcessingDirectionsView,
    ProcessingStockPickerView,
    ProcessingWorkView,
    ProcessingCardView,
    enqueue_processing_print_job,
    processing_print_jobs_next,
    processing_print_jobs_complete,
    download_processing_print_agent_cmd,
    download_processing_print_agent_install_cmd,
    download_processing_print_agent_script,
    delete_processing_draft,
)

app_name = "processing_app"

urlpatterns = [
    path("", ProcessingHomeView.as_view(), name="processing-home"),
    path("directions/", ProcessingDirectionsView.as_view(), name="processing-directions"),
    path("stock/", ProcessingStockPickerView.as_view(), name="processing-stock-picker"),
    path("draft/<str:order_id>/delete/", delete_processing_draft, name="processing-draft-delete"),
    path("print-jobs/", enqueue_processing_print_job, name="processing-print-job"),
    path("print-jobs/next/", processing_print_jobs_next, name="processing-print-jobs-next"),
    path("print-jobs/complete/", processing_print_jobs_complete, name="processing-print-jobs-complete"),
    path("print-agent/download/", download_processing_print_agent_cmd, name="processing-print-agent-download"),
    path("print-agent/install/", download_processing_print_agent_install_cmd, name="processing-print-agent-install"),
    path("print-agent/script/", download_processing_print_agent_script, name="processing-print-agent-script"),
    path("<str:order_id>/work/", ProcessingWorkView.as_view(), name="processing-work"),
    path("<str:order_id>/card/<str:card_id>/", ProcessingCardView.as_view(), name="processing-card"),
    path("<str:order_id>/", ProcessingDetailView.as_view(), name="processing-detail"),
]
