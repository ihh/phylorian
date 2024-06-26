import json
from jsonargparse import CLI

import jax.numpy as jnp

import cigartree
import likelihood
import h20

def main (modelFilename: str,
          treeFilename: str,
          alignFilename: str,
          ):
    """
    Compute the log likelihood of a tree given an alignment and a model.
    
    Args:
        treeFilename: Newick tree file
        alignFilename: FASTA alignment file
        modelFilename: Historian-format JSON file with model parameters
    """
    with open(modelFilename, 'r') as f:
        modelJson = json.load (f)
    with open(treeFilename, 'r') as f:
        treeStr = f.read()
    with open(alignFilename, 'r') as f:
        alignStr = f.read()

    ct = cigartree.makeCigarTree (treeStr, alignStr)

    alphabet, mixture, indelParams, *_others = likelihood.parseHistorianParams (modelJson)
    seqs, _nodeName, distanceToParent, parentIndex, transCounts = cigartree.getHMMSummaries (treeStr, alignStr, alphabet)

    assert len(mixture) == 1, "Only one mixture component is supported for substitution model"
    assert len(indelParams) == 1, "Only one indel parameter set is supported for indel model"
    indelParams = indelParams[0]
    subRate, rootProb = mixture[0]
    subll = likelihood.subLogLike (seqs, distanceToParent, parentIndex, subRate, rootProb)
    subll_total = float (jnp.sum (subll))

    transMat = jnp.stack ([h20.dummyRootTransitionMatrix()] + [h20.transitionMatrix(t,indelParams,alphabetSize=len(alphabet)) for t in distanceToParent[1:]], axis=0)
    transMat = jnp.log (jnp.maximum (transMat, h20.smallest_float32))
    transll = transCounts * transMat
    transll_total = float (jnp.sum (transll))

    print (json.dumps({'loglike':{'subs':subll_total,'indels':transll_total}, 'cigartree': ct}))

if __name__ == '__main__':
    CLI(main, parser_mode='jsonnet')