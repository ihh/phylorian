# Patrick Kidger's bounded_while_loop
# https://github.com/google/jax/issues/8239#issue-1027850539

import jax
import jax.lax as lax
import jax.numpy as jnp

def bounded_optimize(score_fun, update_fun, init_state, max_steps, min_inc=1e-3):
    def cond_fun (args):
        prev_score, (current_score, current_state), (best_score, best_state) = args
        inc = (current_score - prev_score) / jnp.abs(prev_score)
#        jax.debug.print("prev_score={prev_score} current_score={current_score} best_score={best_score} inc={inc}", prev_score=prev_score, current_score=current_score, best_score=best_score, inc=inc)
        return jnp.all (jnp.array ([current_score > prev_score, 
                                    jnp.any (jnp.array([prev_score == -jnp.inf,
                                                        inc > min_inc]))]))
    def body_fun (args):
        prev_score, (current_score, current_state), (best_score, best_state) = args
        prev_score = current_score
        current_state = update_fun (current_state)
        current_score = score_fun (current_state)
        keep = lambda a, b: lax.select(current_score > best_score, a, b)
        best_score, best_state = jax.tree_util.tree_map(keep, (current_score, current_state), (best_score, best_state))
        return prev_score, (current_score, current_state), (best_score, best_state)
    init_score_state = (score_fun(init_state), init_state)
    best_score, best_state = bounded_while_loop (cond_fun, body_fun, (-jnp.inf, init_score_state, init_score_state), max_steps)[2]
    return best_score, best_state


def bounded_while_loop(cond_fun, body_fun, init_val, max_steps):
    """API as `lax.while_loop`, except that it takes an integer `max_steps` argument."""
    if not isinstance(max_steps, int) or max_steps < 0:
        raise ValueError("max_steps must be a non-negative integer")
    if max_steps == 0:
        return init_val
    if max_steps & (max_steps - 1) != 0:
        raise ValueError("max_steps must be a power of two")

    init_data = (cond_fun(init_val), init_val)
    _, val = _while_loop(cond_fun, body_fun, init_data, max_steps)
    return val

def _while_loop(cond_fun, body_fun, data, max_steps):
    if max_steps == 1:
        pred, val = data
        new_val = body_fun(val)
        keep = lambda a, b: lax.select(pred, a, b)
        new_val = jax.tree_util.tree_map(keep, new_val, val)
        return cond_fun(new_val), new_val
    else:

        def _call(_data):
            return _while_loop(cond_fun, body_fun, _data, max_steps // 2)

        def _scan_fn(_data, _):
            _pred, _ = _data
            return lax.cond(_pred, _call, lambda x: x, _data), None

        return lax.scan(_scan_fn, data, xs=None, length=2)[0]