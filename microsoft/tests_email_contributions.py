from types import SimpleNamespace
from django.test import SimpleTestCase
from microsoft.services.email_contributions import EmailContributionParser as Parser, digest


class ContributionParserTests(SimpleTestCase):
    def test_first_html_forward_preserves_history_and_table_cells(self):
        source = {'body_html': '<p>New</p><blockquote>Old unseen message<table><tr><td>FY26</td><td>120</td></tr></table></blockquote>'}
        text = '\n'.join(p.text for p in Parser.parse(source, email_id='1'))
        self.assertIn('Old unseen message', text)
        self.assertIn('FY26 120', text)

    def test_known_span_does_not_remove_unseen_tail_or_middle(self):
        old = 'Previously saved substantive message about the investment and operating performance. ' * 2
        old = old.strip()
        known = SimpleNamespace(text=old, fingerprint=digest(old))
        result = Parser.parse({'body_text': 'New before\n' + old + '\nNew after'}, email_id='forward-id', known=[known])
        self.assertEqual([r.text for r in result], ['New before', old, 'New after'])
        self.assertEqual(result[1].fingerprint, known.fingerprint)

    def test_short_repeated_phrase_and_changed_number_are_retained(self):
        known = SimpleNamespace(text='Yes', fingerprint='known')
        result = Parser.parse({'body_text': 'Yes\nAmount is 101'}, email_id='2', known=[known])
        self.assertIn('Amount is 101', result[0].text)
        self.assertNotEqual(result[0].fingerprint, 'known')

    def test_plain_forward_keeps_headers_and_all_contributions(self):
        source = 'New reply\nFrom: a@example.test\nSent: Monday\nSubject: Opportunity\nOld body\nInline new answer'
        result = Parser.parse({'body_text': source}, email_id='3')
        self.assertEqual('\n'.join(p.text for p in result), source)

    def test_multiline_outlook_from_header_starts_a_forwarded_contribution(self):
        source = 'New reply\nFrom:\nAlice Example <alice@example.test>\nSent: Monday\nSubject: Opportunity\nOld body'
        result = Parser.parse({'body_text': source}, email_id='3')
        self.assertEqual(len(result), 2)
        self.assertEqual(result[1].headers['from'], 'Alice Example <alice@example.test>')
        self.assertIn('Old body', result[1].text)

    def test_source_provenance_survives_embedded_forward_headers(self):
        result = Parser.parse({
            'body_text': 'Wrapper\nFrom: sender@example.test\nSent: Monday\nSubject: Old\nBody',
            'source_email_id': '42',
            'source_graph_id': 'graph-42',
        }, email_id='42')
        self.assertEqual(result[1].headers['email_id'], '42')
        self.assertEqual(result[1].headers['graph_id'], 'graph-42')

    def test_unknown_identity_is_not_deduplicated_across_mail(self):
        a = Parser.parse({'body_text': 'Yes'}, email_id='1')[0]
        b = Parser.parse({'body_text': 'Yes'}, email_id='2')[0]
        self.assertNotEqual(a.fingerprint, b.fingerprint)
