import unittest
from real_estate_desktop.search_planning import preserve_entity_names,amenity_terms

class EntityNames(unittest.TestCase):
    def test_shortened_suffix_keeps_original_specific_name(self):self.assertEqual(preserve_entity_names(['青岚实验小学'],['实验小学']),['青岚实验小学'])
    def test_already_complete_names_stay_the_same(self):self.assertEqual(preserve_entity_names(['青岚实验小学'],['青岚实验小学']),['青岚实验小学'])
    def test_generic_linking_phrase_is_not_treated_as_a_school_name(self):self.assertEqual(preserve_entity_names(amenity_terms('必须同时有小学和门诊')[1],[]),[])
    def test_model_cannot_drop_a_specific_source_name(self):self.assertEqual(preserve_entity_names(['青岚实验小学'],[]),['青岚实验小学'])
