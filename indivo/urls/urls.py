from django.conf.urls import patterns, include

from indivo.views import *
from indivo.lib.utils import MethodDispatcher

urlpatterns = patterns('',
    
    # OAuth
    (r'^oauth/', include('indivo.urls.oauth')),
    (r'^version$', MethodDispatcher({'GET':get_version})),

    # account-specific URLs
    (r'^accounts/$', MethodDispatcher({'POST':account_create})),
    (r'^accounts/search$', MethodDispatcher({'GET':account_search})),
    (r'^accounts/(?P<account_email>[^/]+)$', 
     MethodDispatcher({'GET':account_info})),
    (r'^accounts/(?P<account_email>[^/]+)/', include('indivo.urls.account')),

    # carenet-specific URLs
    (r'^carenets/(?P<carenet_id>[^/]+)', include('indivo.urls.carenet')),

    # record-specific URLs
    (r'^records/$', MethodDispatcher({'POST': record_create})),
    (r'^records/external/(?P<principal_email>[^/]+)/(?P<external_id>[^/]+)$', 
     MethodDispatcher({'PUT'  : record_create_ext})),
    (r'^records/search$', MethodDispatcher({'GET':record_search})),                       
    (r'^records/(?P<record_id>[^/]+)', include('indivo.urls.record')),
    
    # PHAs
    (r'^apps/$', MethodDispatcher({'GET':all_phas})),
    (r'^apps/manifests/$', MethodDispatcher({'GET': all_manifests})),
    (r'^apps/(?P<pha_email>[^/]+)$', 
     MethodDispatcher({'GET' : pha, 'DELETE': pha_delete})),
    (r'^apps/(?P<pha_email>[^/]+)', include('indivo.urls.application')),

    # SMART container calls
    (r'^ontology$', MethodDispatcher({'GET': smart_ontology})),
    (r'^manifest$', MethodDispatcher({'GET': smart_manifest})),              
    
    # static
    (r'^static/(?P<path>.*)$', 'django.views.static.serve', {'document_root': 'static'}),
    
    )
