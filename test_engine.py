import unittest,json
from pathlib import Path
from engine import predict,fair_odds,quarter_kelly
from sync import ref_match,team_match,event_ref
D=json.loads((Path(__file__).parent/'football_model_data.json').read_text())
class Tests(unittest.TestCase):
    def test_probabilities(self):
        for lg,league in D['leagues'].items():
            names=list(league['teams']); r=predict(league,names[0],names[1]);self.assertAlmostEqual(sum(r['1X2']),1,places=9)
            self.assertTrue(all(0<=p<=1 for p in r['1X2']))
    def test_missing_referee_stats(self):
        league=D['leagues']['en.1'];names=list(league['teams']);r=predict(league,names[0],names[1],'Unlisted Referee')
        self.assertEqual(r['referee_factor'],1);self.assertFalse(r['referee_stats_available'])
    def test_abbreviated_referee(self):
        self.assertEqual(ref_match('Brooks J.',{'John Brooks':{}}),'John Brooks')
    def test_no_ambiguous_match(self):
        self.assertIsNone(team_match('United', ['Manchester United FC','Leeds United FC']))
    def test_no_fabricated_official(self):
        self.assertIsNone(event_ref({'competitions':[{'officials':[]}]}))
    def test_odds(self):
        self.assertEqual(fair_odds(.5),2);self.assertEqual(quarter_kelly(.1,2,100),0)
if __name__=='__main__':unittest.main()
