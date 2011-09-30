"""
Records for SMART Reference EMR

Ben Adida & Josh Mandel
"""

from base import *
from django.utils import simplejson
from smart.client.common.query_builder import SMART_Querier
from smart.client.common.rdf_ontology import ontology
from smart.client.common.util import rdf, foaf, vcard, sp, serialize_rdf, parse_rdf, bound_graph, URIRef, Namespace
from smart.lib import utils
from smart.models.apps import *
from smart.models.accounts import *
from smart.models.rdf_store import DemographicConnector, RecordStoreConnector
from smart.models.mongo import key_to_mongo, records_db, extract_field, RDFifier
from string import Template
import re, datetime

class Record(Object):
  Meta = BaseMeta()

  full_name = models.CharField(max_length = 150, null= False)

  def __unicode__(self):
    return 'Record %s' % self.id
  
  def generate_direct_access_token(self, account, token_secret=None):
    u = RecordDirectAccessToken.objects.create(record=self, 
                                               account=account,
                                               token_secret=token_secret)
    u.save()
    return u

  @classmethod
  def search_records_mongo(cls, match):
    ret = []
    for m in  records_db[str(sp.Demographics)].find(match):
      ret.append(m)
      
    return RDFifier(ret).graph.serialize()
    
  """
      r = Record()
      print "Matched", m
      r.fn = extract_field(m, vcard['n'], vcard['given-name'])
      r.ln = extract_field(m, vcard['n'], vcard['family-name'])
      r.dob = extract_field(m, vcard['bday'])
      r.gender = extract_field(m, foaf['gender'])
      r.zipcode = extract_field(m, vcard['adr'], vcard['postal-code'])[0]
      ret.append(r)
    return ret
    """

  @classmethod
  def search_records(cls):
    ret = []
    for m in  records_db[str(sp.Demographics)].find():
      r = Record()
      r.id  = m['@subject']['@iri'].rsplit("/records/", 1)[1].split("/demographics")[0]
      r.fn = extract_field(m, vcard['n'], vcard['given-name'])
      r.ln = extract_field(m, vcard['n'], vcard['family-name'])
      r.dob = extract_field(m, vcard['bday'])
      r.gender = extract_field(m, foaf['gender'])
      r.zipcode = extract_field(m, vcard['adr'], vcard['postal-code'])[0]
      ret.append(r)
    return ret
    
class AccountApp(Object):
  account = models.ForeignKey(Account)
  app = models.ForeignKey(PHA)

  # uniqueness
  class Meta:
    app_label = APP_LABEL
    unique_together = (('account', 'app'),)


# Not an OAuth token, but an opaque token
# that can be used to support auto-login via a direct link
# to a smart_ui_server. 
class RecordDirectAccessToken(Object):
  record = models.ForeignKey(Record, related_name='direct_access_tokens', null=False)
  account = models.ForeignKey(Account, related_name='direct_record_shares', null=False)
  token = models.CharField(max_length=40, unique=True)
  token_secret = models.CharField(max_length=60, null=True)
  expires_at = models.DateTimeField(null = False)

  def save(self, *args, **kwargs):

    if not self.token:
      self.token = utils.random_string(30)
      print "RANDOM", self.token


    if self.expires_at == None:
      minutes_to_expire=30
      try:
        minutes_to_expire = settings.MINUTES_TO_EXPIRE_DIRECT_ACCESS
      except: pass

      self.expires_at = datetime.datetime.utcnow() + datetime.timedelta(minutes = minutes_to_expire)
    super(RecordDirectAccessToken, self).save(*args, **kwargs)

  class Meta:
    app_label = APP_LABEL

class RecordAlert(Object):
  record=models.ForeignKey(Record)
  alert_text =  models.TextField(null=False)
  alert_time = models.DateTimeField(auto_now_add=True, null=False)
  triggering_app = models.ForeignKey('OAuthApp', null=False, related_name='alerts')
  acknowledged_by = models.ForeignKey('Account', null=True)
  acknowledged_at = models.DateTimeField(null=True)

  # uniqueness
  class Meta:
    app_label = APP_LABEL
  
  @classmethod
  def from_rdf(cls, rdfstring, record, app):
    s = parse_rdf(rdfstring)

    q = """
    PREFIX rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#>
    PREFIX sp: <http://smartplatforms.org/terms#>
    SELECT ?notes ?severity
    WHERE {
          ?a rdf:type sp:Alert.
          ?a sp:notes ?notes.
          ?a sp:severity ?scv.
          ?scv sp:code ?severity.
    }"""

    r = list(s.query(q))
    assert len(r) == 1, "Expected one alert in post, found %s"%len(r)
    (notes, severity) = r[0]

    assert type(notes) == Literal
    spcodes = Namespace("http://smartplatforms.org/terms/code/alertLevel#")
    assert severity in [spcodes.information, spcodes.warning, spcodes.critical]

    a = RecordAlert(record=record, 
                    alert_text=str(notes), 
                    triggering_app=app)
    a.save()
    return a

  def acknowledge(self, account):
    self.acknowledged_by =  account
    self.acknowledged_at = datetime.datetime.now()
    self.save()

class LimitedAccount(Account):
      records = models.ManyToManyField(Record, related_name="+")
