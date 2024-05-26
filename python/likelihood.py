import jax
import jax.numpy as jnp
from jax.scipy.linalg import expm

def alignmentIsValid (alignment, alphabetSize):
    assert jnp.all(alignment >= -1)
    assert jnp.all(alignment < alphabetSize)

def treeIsValid (distanceToParent, parentIndex, alignmentRows):
    assert jnp.all(distanceToParent >= 0)
    assert jnp.all(parentIndex[1:] >= -1)
    assert jnp.all(parentIndex <= jnp.arange(alignmentRows))

# Compute substitution log-likelihood of a multiple sequence alignment and phylogenetic tree by pruning
# Parameters:
#  - alignment: (R,C) integer tokens. C is the length of the alignment (#cols), R is the number of sequences (#rows). A token of -1 indicates a gap.
#  - distanceToParent: (R,) floats, distance to parent node
#  - parentIndex: (R,) integers, index of parent node. Nodes are sorted in preorder so parentIndex[i] <= i for all i. parentIndex[0] = -1
#  - subRate: (*H,A,A) substitution rate matrix/matrices. Leading H axes (if any) are "hidden" substitution rate categories, A is alphabet size
#  - rootProb: (*H,A) root frequencies (typically equilibrium frequencies for substitution rate matrix)
# To pad rows, set parentIndex[paddingRow:] = arange(paddingRow,R)
# To pad columns, set alignment[paddingRow,paddingCol:] = -1

def subLogLike (alignment, distanceToParent, parentIndex, subRate, rootProb):
    subMatrix = computeSubMatrixForBranchLengths (distanceToParent, subRate)
    return subLogLikeForMatrices (alignment, parentIndex, subMatrix, rootProb)

def computeSubMatrixForBranchLengths (distanceToParent, subRate):
    assert distanceToParent.ndim == 1
    assert subRate.ndim >= 2
    R, = distanceToParent.shape
    *H, A = subRate.shape[0:-1]
    assert subRate.shape == (*H,A,A)
    # Compute transition matrices per branch
    subMatrix = expm (jnp.einsum('...ij,r->...rij', subRate, distanceToParent))  # (*H,R,A,A)
    return subMatrix

defaultDiscretizationParams = (1e-3, 10, 400)  # tMin, tMax, nSteps
def getDiscretizedTimes (discretizationParams=defaultDiscretizationParams):
    return jnp.concat ([jnp.array([0]), jnp.geomspace (*discretizationParams)])

def computeSubMatrixForDiscretizedTimes (subRate, discretizationParams=defaultDiscretizationParams):
    t = getDiscretizedTimes (discretizationParams)
    discreteTimeSubMatrix = computeSubMatrixForBranchLengths (t, subRate)  # (*H,T,A,A)
    return discreteTimeSubMatrix

def discretizeBranchLength (t, discretizationParams=defaultDiscretizationParams):
    tMin, tMax, nSteps = discretizationParams
    return jnp.where (t == 0,
                      0,
                      1 + jnp.digitize (jnp.clip (t, tMin, tMax), jnp.geomspace (tMin, tMax, nSteps)))

def computeSubMatrixForDiscretizedBranchLengths (t, discreteTimeSubMatrix, discretizationParams=defaultDiscretizationParams):
    subMatrix = discreteTimeSubMatrix[...,discretizeBranchLength(t),:,:]  # (*H,R,A,A)
    return subMatrix

def subLogLikeForMatrices (alignment, parentIndex, subMatrix, rootProb):
    assert alignment.ndim == 2
    assert subMatrix.ndim >= 3
    *H, R, A = subMatrix.shape[0:-1]
    C = alignment.shape[-1]
    assert parentIndex.shape == (R,)
    assert rootProb.shape == (*H,A)
    assert subMatrix.shape == (*H,R,A,A)
    assert alignment.dtype == jnp.int32
    assert parentIndex.dtype == jnp.int32
    # Initialize pruning matrix
    tokenLookup = jnp.concatenate([jnp.ones(A)[None,:],jnp.eye(A)])
    likelihood = tokenLookup[alignment + 1]  # (R,C,A)
    if len(H) > 0:
        likelihood = jnp.expand_dims (likelihood, jnp.arange(len(H)))  # (*ones_like(H),R,C,A)
        likelihood = jnp.repeat (likelihood, repeats=jnp.array(H), axis=jnp.arange(len(H)))  # (*H,R,C,A)
    logNorm = jnp.zeros(*H,C)  # (*H,C)
    # Compute log-likelihood for all columns in parallel by iterating over nodes in postorder
    for child in range(R-1,0,-1):
        parent = parentIndex[child]
        likelihood = likelihood.at[...,parent,:,:].multiply (jnp.einsum('...ij,...cj->...ci', subMatrix[...,child,:,:], likelihood[...,child,:,:]))
        maxLike = jnp.max(likelihood[...,parent,:,:], axis=-1)  # (*H,C)
        likelihood = likelihood.at[...,parent,:,:].divide (maxLike[...,None])  # guard against underflow
        logNorm = logNorm + jnp.log(maxLike)
    logNorm = logNorm + jnp.log(jnp.einsum('...ci,...i->...c', likelihood[...,0,:,:], rootProb))  # (*H,C)
    return logNorm

def padDimension (len, multiplier):
    return jnp.where (multiplier == 2,  # handle this case specially to avoid precision errors
                      1 << (len-1).bit_length(),
                      int (jnp.ceil (multiplier ** jnp.ceil (jnp.log(len) / jnp.log(multiplier)))))

def padAlignment (alignment, parentIndex, distanceToParent, nRows: int = None, nCols: int = None, colMultiplier = 2, rowMultiplier = 2):
    unpaddedRows, unpaddedCols = alignment.shape
    if nCols is None:
        nCols = padDimension (unpaddedCols, colMultiplier)
    if nRows is None:
        nRows = padDimension (unpaddedRows, rowMultiplier)
    # To pad rows, set parentIndex[paddingRow:] = arange(paddingRow,R) and pad alignment and distanceToParent with any value (-1 and 0 for predictability)
    if nRows > unpaddedRows:
        alignment = jnp.concatenate ([alignment, -1*jnp.ones((nRows - unpaddedRows, unpaddedCols), dtype=alignment.dtype)], axis=0)
        distanceToParent = jnp.concatenate ([distanceToParent, jnp.zeros(nRows - unpaddedRows, dtype=distanceToParent.dtype)], axis=0)
        parentIndex = jnp.concatenate ([parentIndex, jnp.arange(unpaddedRows,nRows, dtype=parentIndex.dtype)], axis=0)
    # To pad columns, set alignment[paddingRow,paddingCol:] = -1
    if nCols > unpaddedCols:
        alignment = jnp.concatenate ([alignment, -1*jnp.ones((nRows, nCols - unpaddedCols), dtype=alignment.dtype)], axis=1)
    return alignment, parentIndex, distanceToParent

def normalizeSubRate (subRate):
    subRate = jnp.abs (subRate)
    return subRate - jnp.diag(jnp.sum(subRate, axis=-1))

def probsFromLogits (logits):
    return jax.nn.softmax(logits)

def normalizeRootProb (rootProb):
    return rootProb / jnp.sum(rootProb)

def normalizeSubModel (subRate, rootProb):
    return normalizeSubRate(subRate), normalizeRootProb(rootProb)

def parametricSubModel (subRate, rootLogits):
    return normalizeSubRate(subRate), probsFromLogits(rootLogits)

def reversibleSubRate (symSubRate, rootProb):
    sqrtRootProb = jnp.sqrt(rootProb)
    return jnp.einsum('i,...ij,j->...ij', 1/sqrtRootProb, symSubRate, sqrtRootProb)

def symmetrizeSubRate (matrix):
    return normalizeSubRate (0.5 * (matrix + matrix.swapaxes(-1,-2)))

def parametricReversibleSubModel (subRate, rootLogits):
    rootProb = probsFromLogits (rootLogits)
    subRate = reversibleSubRate (symmetrizeSubRate(subRate), rootProb)
    return subRate, rootProb

def parseHistorianParams (params):
    alphabet = params['alphabet']
    def parseSubRate (p):
        subRate = jnp.array([[p['subrate'].get(i,{}).get(j,0) for j in alphabet] for i in alphabet], dtype=jnp.float32)
        rootProb = jnp.array([p['rootprob'].get(i,0) for i in alphabet], dtype=jnp.float32)
        subRate, rootProb = normalizeSubModel (subRate, rootProb)
        return subRate, rootProb
    if 'mixture' in params:
        mixture = [parseSubRate(p) for p in params['mixture']]
    else:
        mixture = [parseSubRate(params)]
    indelParams = tuple(params.get(name,0) for name in ['insrate','delrate','insextprob','delextprob'])
    return alphabet, mixture, indelParams

def toHistorianParams (alphabet, mixture, indelParams):
    def toSubRate (subRate, rootProb):
        return { 'subrate': {alphabet[i]: {alphabet[j]: float(subRate[i,j]) for j in range(len(alphabet)) if i != j} for i in range(len(alphabet))},
                 'rootprob': {alphabet[i]: float(rootProb[i]) for i in range(len(alphabet))} }
    if len(mixture) == 1:
        return toSubRate(*mixture[0]), {name: float(param) for name,param in zip(['insrate','delrate','insextprob','delextprob'],indelParams)}
    else:
        return {'mixture': [toSubRate(*m) for m in mixture]}, {name: float(param) for name,param in zip(['insrate','delrate','insextprob','delextprob'],indelParams)}