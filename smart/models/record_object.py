import re, uuid
from django.conf import settings
from smart.client.common.rdf_ontology import api_types, api_calls, ontology, SMART_Class
from smart.client.common.query_builder import SMART_Querier
from rdf_rest_operations import *
from smart.client.common.util import remap_node, parse_rdf, get_property, LookupType, URIRef, sp, rdf, default_ns
from ontology_url_patterns import CallMapper, BasicCallMapper

class RecordObject(object):
    __metaclass__ = LookupType
    known_types_dict = {}

    get_one = staticmethod(record_get_object)
    get_all = staticmethod(record_get_all_objects)
    delete_one = staticmethod(record_delete_object)
    delete_all = staticmethod(record_delete_all_objects)
    post = staticmethod(record_post_objects)
    put = staticmethod(record_put_object)
    
    def __init__(self, smart_type):
        self.smart_type = smart_type
        RecordObject.register_type(smart_type, self)

    @classmethod
    def __getitem__(cls, key):
        try: return cls.known_types_dict[key]
        except: 
            try: return cls.known_types_dict[key.uri]
            except: 
                return cls.known_types_dict[URIRef(key.encode())]

    @classmethod
    def register_type(cls, smart_type, robj):
        cls.known_types_dict[smart_type.uri] = robj
                
    @property
    def properties(self):
        return [x.property for x in self.smart_type.properties]
    
    @property
    def uri(self):
        return str(self.smart_type.uri)
    
    @property
    def node(self):
        return self.smart_type.uri

    @property
    def path(self):
        v = self.smart_type.base_path
        if v: return str(v)
        return None    
         
    def internal_id(self, record_connector, external_id):
        idquery = """
            PREFIX rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#>
            CONSTRUCT {%s <http://smartplatforms.org/terms#externalIDFor> ?o.}
            FROM $context
            WHERE {
                    %s <http://smartplatforms.org/terms#externalIDFor> ?o.
                  }  """%(external_id.n3(), external_id.n3())
        id_graph = parse_rdf(record_connector.sparql(idquery))


        l = list(id_graph)
        if len(l) > 1:
            raise Exception( "MORE THAN ONE ENTITY WITH EXTERNAL ID %s : %s"%(external_id, ", ".join([str(x[0]) for x in l])))

        try:
            s =  l[0][2]
            return s
        except: 
            return None
        
    def path_var_bindings(self, request_path):
        print request_path, self.path
        var_names =  re.findall("{(.*?)}",self.path)
        
        match_string = self.path
        for i,v in enumerate(var_names):
            # Force all variables to match, except the final one
            # (which can be a new GUID, substituted in on-the-fly.)
            repl = i+1 < len(var_names) and "([^\/]+).*" or "([^\/]*?)"
            match_string = re.sub("{"+v+"}", repl, match_string)
        matches = re.search(match_string, request_path).groups()
        var_values = {}
  
        for i,v in enumerate(var_names):
            if matches[i] != "":
                var_values[v] = matches[i]
  
        return var_values
    
    def determine_full_path(self, var_bindings=None):
        ret = settings.SITE_URL_PREFIX + self.path
        for vname, vval in var_bindings.iteritems():
            if vval == "": vval="{new_id}"
            ret = ret.replace("{"+vname+"}", vval)

        still_unbound = re.findall("{(.*?)}",ret)
        assert len(still_unbound) <= 1, "Can't match path closely enough: %s given %s -- got to %s"%(self.path, var_bindings, ret)
        if len(still_unbound) ==1:
            ret = ret.replace("{"+still_unbound[0]+"}", str(uuid.uuid4()))
        
        return URIRef(ret)    

    def determine_remap_target(self,g,c,s,var_bindings):
        full_path = None

        if type(s) == Literal: return None

        node_type_candidates = list(g.triples((s, rdf.type, None)))
        node_type = None
        for c in node_type_candidates:
            t = SMART_Class[c[2]]
            if t.is_statement or t.uri == sp.MedicalRecord:
                assert node_type==None, "Got multiple node types for %s"%[x[2] for x in node_type]
                node_type  = t.uri

        if type(s) == BNode and not node_type: return None
        elif type(s) == URIRef:
            subject_uri = str(s)        
            if subject_uri.startswith("urn:smart_external_id:"):
                full_path = self.internal_id(c, s)
                assert full_path or node_type != None, "%s is a new external URI node with no type"%s.n3()
            else:
                return None

        # If we got here, we need to remap the node "s".
        if full_path == None:
            full_path = RecordObject[node_type].determine_full_path(var_bindings)
        return full_path

    def generate_uris(self, g, c, var_bindings=None):   
        node_map = {}    
        nodes = set(g.subjects()) | set(g.objects())

        for s in nodes:
            new_node = self.determine_remap_target(g,c,s, var_bindings)
            if new_node: node_map[s] = new_node

        for (old_node, new_node) in node_map.iteritems():
            remap_node(g, old_node, new_node)
            if type(old_node) == URIRef:
                g.add((old_node, sp.externalIDFor, new_node))


        return node_map.values()

    def attach_statements_to_record(self, g, new_uris, var_bindings):
        # Attach each data element (med, problem, lab, etc), to the 
        # base record URI with the sp:Statement predicate.
        recordURI = URIRef(smart_path("/records/%s"%var_bindings['record_id']))
        for n in new_uris:
            node_type = get_property(g, n, rdf.type)
            
            # Filter for top-level medical record "Statement" types
            t = ontology[node_type]
            if (not t.is_statement): continue
            if (not t.base_path.startswith("/records")): continue
            if (n == recordURI): continue # don't assert that the record has itself as an element
            
            g.add((n, sp.belongsTo, recordURI))
            g.add((recordURI, sp.hasStatement, n))
            g.add((recordURI, rdf.type, sp.MedicalRecord))



    def prepare_graph(self, g, c, var_bindings=None):
        new_uris = self.generate_uris(g, c, var_bindings)
        self.attach_statements_to_record(g, new_uris, var_bindings)

    def query_one(self, id):
        ret = SMART_Querier.query_one(self.smart_type, id=id)
        return ret

    def query_all(self):
        ret = SMART_Querier.query_all(self.smart_type)
        return ret

for t in api_types:
    RecordObject(t)

class RecordCallMapper(object):
    def __init__(self, call):
        self.call = call
        self.obj = RecordObject[self.call.target]

    @property
    def get(self): return None
    @property
    def delete(self): return None
    @property
    def post(self): return self.obj.post
    @property
    def put(self): return self.obj.put

    @property
    def map_score(self):
        cat = str(self.call.category)
        if cat.startswith("record") and cat.endswith(self.ending):
            return 1
        return 0

    @property
    def arguments(self):
      r = {'obj': self.obj}      
      return r

    @property
    def maps_to(self):
        m = str(self.call.method)

        if "GET" == m:
            return self.get
        if "PUT" == m:
            return self.put
        if "POST" == m:
            return self.post
        if  "DELETE" == m:
            return self.delete    

        assert False, "Method not in GET, PUT, POST, or DELETE"

@CallMapper.register
class RecordItemsCallMapper(RecordCallMapper):
    @property
    def get(self): return self.obj.get_all
    @property
    def delete(self): return self.obj.delete_all
    ending = "_items"

@CallMapper.register
class RecordItemCallMapper(RecordCallMapper):
    @property
    def get(self): return self.obj.get_one
    @property
    def delete(self): return self.obj.delete_one
    ending = "_item"


@CallMapper.register(category="record_items",
                     method="GET",
                     target="http://smartplatforms.org/terms#LabResult",
                     filter_func=lambda c: str(c.path).find("loinc")>-1)
def record_get_filtered_labs(request, *args, **kwargs):
      record_id = kwargs['record_id']
      loincs = kwargs['comma_separated_loincs'].split(",")

      filters = " || ".join (["?filteredLoinc = <http://loinc.org/codes/%s>"%s 
                              for s in loincs])

      l = RecordObject["http://smartplatforms.org/terms#LabResult"]
      c = RecordStoreConnector(Record.objects.get(id=record_id))
      q =  l.query_all(filter_clause="""
        {
          ?root_subject <http://smartplatforms.org/terms#labName> ?filteredLab.
          ?filteredLab <http://smartplatforms.org/terms#code> ?filteredLoinc.
        }  FILTER (%s)"""%filters
           )
      return rdf_response(c.sparql(q))



@CallMapper.register(category="record_items",
                     method="GET",
                     target="http://smartplatforms.org/terms#Allergy")
def record_get_allergies(request, *args, **kwargs):
      record_id = kwargs['record_id']
      a = RecordObject["http://smartplatforms.org/terms#Allergy"]
      ae = RecordObject["http://smartplatforms.org/terms#AllergyExclusion"]
      c = RecordStoreConnector(Record.objects.get(id=record_id))

      ma = c.sparql(a.query_all())
      m = parse_rdf(ma)

      mae = c.sparql(ae.query_all())
      parse_rdf(mae, model=m)

      return rdf_response(serialize_rdf(m))

@CallMapper.register(category="record_item",
                     method="POST",
                     target="http://smartplatforms.org/terms#Alert")
def record_post_alert(request, *args, **kwargs):
      record_id = kwargs['record_id']
      r = Record.objects.get(id=record_id)
      app = request.principal.share.with_app

      RecordAlert.from_rdf(request.raw_post_data, r, app)
      return rdf_response(request.raw_post_data)
