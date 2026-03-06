import asyncio
import math
import random
import statistics

from abc import ABC, abstractmethod
from dataclasses import dataclass
from functools import partialmethod
from typing import override, final, Mapping, Callable, Iterable

from crs.common.llm_api import split_by_tokens
from crs.common.types import Tool, Message
from crs.common.utils import cached_property
from .agent import AgentGeneric

from crs_rust import logger

class ClassifierResult[K](dict[K, float]):
    def best(self):
        return sorted(self.items(), key=lambda v: v[1])[-1]

@dataclass
class ClassifierBatchResult[K]:
    results: list[ClassifierResult[K]]

    @property
    def keys(self) -> list[K]:
        return list(self.results[0].keys())

    def accumulate(self, k: K, f: Callable[[Iterable[float]], float]):
        return f(p[k] for p in self.results)

    max = partialmethod(accumulate, f=max)
    min = partialmethod(accumulate, f=min)
    sum = partialmethod(accumulate, f=sum)
    var = partialmethod(accumulate, f=statistics.variance)

    def std(self, k: K):
        return math.sqrt(self.var(k))

    def avg(self, k: K):
        return self.sum(k) / len(self.results)

    def cv(self, k: K):
        """
        Coefficient of variation = sigma / mu
        """
        return self.std(k) / self.avg(k)

    def range(self, k: K):
        return self.max(k) - self.min(k)

    def mode(self) -> K:
        return statistics.mode(r.best()[0] for r in self.results)


class Classifier[K](AgentGeneric[ClassifierResult[K]], ABC):
    """
    Generically useful class for classification tasks
    Note: The keys will be `str`'d to decided what the LLM must output to select them.
    You should generally just use strings as keys, but you could use integers as well.

    Note: technically not an "agent", but we share the base class for convenience
    """
    @property
    @abstractmethod
    def details(self) -> str:
        ...

    @cached_property
    @abstractmethod
    def options(self) -> dict[K, str]:
        """
        Maps key => description
        key is the string the LLM must output to select this option
        """
        pass

    @final
    @cached_property
    def tools(self) -> Mapping[str, Tool]:
        return {}

    @final
    @property
    def logprobs(self):
        return self._supports_logprobs

    @final
    @property
    def top_logprobs(self):
        if not self._supports_logprobs:
            return None
        # assume there are ~2 possible tokens to continue any given option
        # but, the openai API rejects values above 20
        return min(20, 2 * len(self.options))

    @final
    @property
    def temperature(self):
        return 0

    @final
    @property
    def max_completion_tokens(self):
        if not self._supports_logprobs:
            # Non-logprobs models may produce a few tokens
            return 10
        # Since we assert all options are single-token, we only need 1 token
        return 1

    @property
    def _supports_logprobs(self) -> bool:
        return "gpt" in super().model

    @property
    def model(self):
        """
        Calls the base class model property to support model config maps.
        Non-GPT models (e.g. Claude) don't support logprobs, but we handle
        that in _iter by falling back to text-based classification.
        """
        return super().model

    def get_result(self, msg: Message) -> None:
        raise NotImplementedError # should never be called because we override _run

    def re_normalize_dict(self, prob_dict: dict[K, float]) -> dict[K, float]:
        sum_values = sum(prob_dict.values())

        if sum_values == 0:
            return prob_dict
        return {k: v / sum_values for k, v in prob_dict.items()}
    
    async def run(self, max_iters: int = 1):
        return await super().run(max_iters=max_iters)

    @override
    async def _iter(self, max_iters: int = 1) -> ClassifierResult[K]:
        assert max_iters == 1, "cannot change max_iters in Classifier"
        completion = (await self.completion()).unwrap() # no choice but to unwrap
        choice = completion.choices[0]

        if self._supports_logprobs:
            logprobs = choice.logprobs
            assert logprobs is not None
            assert len(logprobs.content) == 1, "logprobs should have only one content"

            # compute logprob of each classifier option
            keys = {str(k): k for k in self.options}
            for k in keys:
                assert len(await asyncio.to_thread(split_by_tokens, self.model, k)) == 1, "All options must be single-token"

            logger.debug("classifier logprobs: {logprobs}", logprobs=logprobs)

            key_probs = {keys[x.token]: math.exp(x.logprob) for x in logprobs.content[0].top_logprobs if x.token in keys}
            _res = {k: key_probs.get(k, 0) for k in self.options}
        else:
            # Non-logprobs fallback: parse the text response and assign p=1.0
            # to the matched option. Works with Claude and other non-GPT models.
            text = (choice.message.content or "").strip()
            keys = {str(k): k for k in self.options}
            matched = keys.get(text)
            if matched is None:
                # Try to find the option token anywhere in the response
                for token, key in keys.items():
                    if token in text:
                        matched = key
                        break
            if matched is None:
                # Case-insensitive search
                text_lower = text.lower()
                for token, key in keys.items():
                    if token.lower() in text_lower:
                        matched = key
                        break
            if matched is None:
                # Default to last option (typically 'NEW' for dedup)
                matched = list(self.options)[-1]
                logger.warning(f"classifier text fallback: no match in '{text}', defaulting to {matched}")
            _res = {k: (1.0 if k == matched else 0.0) for k in self.options}

        res: ClassifierResult[K] = ClassifierResult(self.re_normalize_dict(_res))
        logger.info(f"classifier result: {str(res)}")
        self._append_msg(completion.choices[0].message)
        self.terminated = True
        return res

    async def classify(self) -> ClassifierResult[K]:
        res = await self.run()
        assert res.response is not None, "agent response should never be None"
        return res.response

    # type ignore justification: classmethod typing doesn't accept our cls type annotation,
    # but annotating cls this way is required to allow type inference of P at the call sites
    @classmethod # type: ignore
    async def batch_classify[**P](
        cls: Callable[P, 'Classifier[K]'],
        batch_size: int,
        *args: P.args,
        **kwargs: P.kwargs
    ):
        """
        Runs a batch of classifiers and aggregates the results.

        Useful for challenging classification tasks, where results can vary greatly
        based on uncontrollable factors, such as MoE scheduling. The distribution of
        results can be useful for measuring task difficulty or model uncertainty.

        Note: it could be more efficient make a single completion call with
        `n=batch_size`, but empirically that often hides most of the variance.
        Using separate completion calls also allows separate prompt seeding,
        reducing the odds of caching.
        """
        instances = [cls(*args, **kwargs) for _ in range(batch_size)]
        async with asyncio.TaskGroup() as tg:
            results = await asyncio.gather(*[
                tg.create_task(inst.classify(), name=f'classify_batch_{i}')
                for i, inst in enumerate(instances)
            ])
        return ClassifierBatchResult(results)

    def __init__(self):
        self.prompt_seed = random.randbytes(10).hex()
        super().__init__()
